"""ECC refinement of a coarse transform (plan B5, task 4.8).

``refine_ecc`` takes an initial 3x3 old->new similarity (or affine) matrix and
polishes it with ``cv2.findTransformECC`` over a three-level pyramid, seeded
coarse-to-fine. Design notes from the plan and the empirical work behind this
module:

* **Blur first.** ECC on raw line art converges poorly; a Gaussian pre-blur
  (sigma ~1.5) smooths the correlation surface. cv2's internal
  ``gaussFiltSize`` filter is used on top (5 px, empirically the best of the
  valid sizes on synthetic line art).
* **Argument order.** In OpenCV 5.0 ``findTransformECC(template, input,
  seed)`` returns the warp that maps the *template* onto the *input* (verified
  empirically: rendering the template with the returned warp reproduces the
  input pixel-exactly). The refined old->new matrix is therefore the inverse
  of the returned warp; the seed is the inverse of the current old->new
  estimate.
* **Never return a worse transform.** ECC can fail to converge or diverge on
  line art, so every ECC call runs with a hard wall-clock timeout
  (``config.ecc_timeout_s``) on a daemon thread, and the result is only
  accepted when an ink-distance RMS residual over the sheet measurably
  improves over the input transform. A timed-out ECC call keeps running on its
  daemon thread until cv2 returns; it cannot be killed, which is the
  documented trade-off of a hard timeout.
* **Iterations.** cv2 does not report how many internal iterations ECC
  consumed, so ``RefineResult.iterations`` counts the pyramid levels that ran
  and ``epsilon_final`` is NaN; ``correlation`` carries the final ECC
  correlation coefficient, which is the meaningful quality number.

The RMS residuals are chamfer-style: the root-mean-square distance from old
ink samples (warped by the matrix under test) to the nearest new ink pixel,
measured on a subsampled copy when the sheets are large and reported in
full-resolution pixels. Zero for a perfect alignment, a few pixels for a
translation a few pixels off - the right behaviour for the never-worse gate.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.types import AlignConfig, TransformModel

#: cv2's internal ECC Gaussian filter size (odd, > 0; -1 fails in OpenCV 5.0).
ECC_GAUSS_FILT = 5
#: Pyramid level factors, coarse first; each level seeds the next.
PYRAMID_LEVELS = (0.25, 0.5, 1.0)
#: Refinement runs on a canvas no larger than this (pixels on the long side).
ECC_MAX_SIDE = 500.0
#: Blur sigma applied before every ECC call (plan B5).
ECC_BLUR_SIGMA = 1.5
#: RMS metric samples the sheets at no more than this long side.
RMS_MAX_SIDE = 800.0


@dataclass(slots=True)
class RefineResult:
    """Outcome of one ECC refinement attempt.

    ``matrix`` is either the accepted refined matrix (``applied`` is True) or
    the input matrix unchanged - never a worse transform than given. RMS
    values are full-resolution pixels on the sheets passed in.
    """

    matrix: NDArray[np.float64]
    applied: bool
    #: Pyramid levels that completed (cv2 hides its internal iteration count).
    iterations: int
    #: NaN: cv2 does not expose the final epsilon of the ECC iterations.
    epsilon_final: float
    #: Final ECC correlation coefficient (higher is better; ~1.0 = identical).
    correlation: float
    rms_before_px: float
    rms_after_px: float


def _as_gray(img: np.ndarray) -> NDArray[np.uint8]:
    """Grayscale uint8 version of an image (2D, BGR or BGRA input)."""
    arr = np.asarray(img)
    if arr.ndim == 2:
        gray = arr
    elif arr.ndim == 3 and arr.shape[2] == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
    else:
        raise ValueError(f"unsupported image shape {arr.shape}")
    return gray.astype(np.uint8) if gray.dtype != np.uint8 else gray


def _ecc_worker(
    template: np.ndarray,
    input_img: np.ndarray,
    seed: np.ndarray,
    motion: int,
    criteria: tuple[int, int, float],
    result: dict[str, object],
) -> None:
    """Run one findTransformECC call; store 'warp'/'correlation' or 'error'."""
    try:
        correlation, warp = cv2.findTransformECC(
            template, input_img, seed, motion, criteria, None, ECC_GAUSS_FILT
        )
        result["warp"] = warp
        result["correlation"] = float(correlation)
    except Exception as exc:  # cv2.error on degenerate input
        result["error"] = exc


def _ecc_with_timeout(
    template: np.ndarray,
    input_img: np.ndarray,
    seed: np.ndarray,
    motion: int,
    criteria: tuple[int, int, float],
    timeout_s: float,
) -> tuple[np.ndarray, float] | None:
    """Run ECC on a daemon thread with a hard timeout.

    Returns (warp, correlation) or None on timeout. A timed-out cv2 call
    keeps running on the daemon thread (it cannot be interrupted); the thread
    is a daemon so it never blocks interpreter exit. ECC exceptions are
    re-raised for the caller to treat as a failed level.
    """
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=_ecc_worker,
        args=(template, input_img, seed, motion, criteria, result),
        name="refine-ecc",
        daemon=True,
    )
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    warp = result["warp"]
    correlation = result["correlation"]
    if not isinstance(warp, np.ndarray):
        raise RuntimeError("ECC returned no warp matrix")
    return warp, float(correlation)


def _ink_mask(gray: np.ndarray) -> NDArray[np.uint8]:
    """Binary ink mask (255 = ink) via Otsu thresholding."""
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]


def _ink_rms_px(old_gray: np.ndarray, new_gray: np.ndarray, matrix: np.ndarray) -> float:
    """Chamfer RMS (px) from old ink warped by *matrix* to the nearest new ink.

    Sampled on copies with the long side capped at RMS_MAX_SIDE; the returned
    value is in full-resolution pixels of the input images. Points that warp
    outside the new sheet are excluded; a matrix that flings most ink outside
    reports +inf so the never-worse gate rejects it.
    """
    old_h, old_w = old_gray.shape[:2]
    new_h, new_w = new_gray.shape[:2]
    sample = min(1.0, RMS_MAX_SIDE / max(old_h, old_w, new_h, new_w))
    old_small = cv2.resize(old_gray, None, fx=sample, fy=sample, interpolation=cv2.INTER_AREA)
    new_small = cv2.resize(new_gray, None, fx=sample, fy=sample, interpolation=cv2.INTER_AREA)
    ink_old = _ink_mask(old_small)
    ink_new = _ink_mask(new_small)
    ys, xs = np.nonzero(ink_old)
    if len(ys) == 0:
        return float("inf")
    # cv2.distanceTransform measures distance to the nearest *zero* pixel, so
    # the ink mask must be inverted: ink pixels read 0, everything else reads
    # its distance to the nearest ink.
    distances = cv2.distanceTransform(cv2.bitwise_not(ink_new), cv2.DIST_L2, 5).astype(np.float32)
    small_matrix = matrix.copy()
    small_matrix[0, 2] *= sample
    small_matrix[1, 2] *= sample
    points = np.column_stack([xs.astype(float), ys.astype(float)])
    warped = points @ small_matrix[:2, :2].T + small_matrix[:2, 2]
    xi = np.rint(warped[:, 0]).astype(int)
    yi = np.rint(warped[:, 1]).astype(int)
    inside = (xi >= 0) & (xi < new_small.shape[1]) & (yi >= 0) & (yi < new_small.shape[0])
    if inside.sum() < 0.25 * len(points):
        return float("inf")
    residual = distances[yi[inside], xi[inside]].astype(float)
    return float(np.sqrt(np.mean(residual**2)) / sample)


def _to_3x3(warp2x3: np.ndarray) -> NDArray[np.float64]:
    matrix = np.eye(3, dtype=float)
    matrix[:2, :] = warp2x3
    return matrix


def _no_change_result(
    initial: np.ndarray,
    rms_before: float,
    correlation: float = float("nan"),
    iterations: int = 0,
) -> RefineResult:
    return RefineResult(
        matrix=np.asarray(initial, dtype=float),
        applied=False,
        iterations=iterations,
        epsilon_final=float("nan"),
        correlation=correlation,
        rms_before_px=rms_before,
        rms_after_px=rms_before if math.isfinite(rms_before) else float("nan"),
    )


def refine_ecc(
    old_img: np.ndarray,
    new_img: np.ndarray,
    initial: np.ndarray,
    model: TransformModel = TransformModel.SIMILARITY,
    *,
    config: AlignConfig | None = None,
) -> RefineResult:
    """Refine *initial* (3x3 old->new) with pyramid ECC; see module docstring.

    SIMILARITY maps to ``MOTION_EUCLIDEAN`` (rotation + translation; note that
    ECC's euclidean model carries no scale, so any scale in *initial* is
    preserved and only its rotation/translation is refined). AFFINE maps to
    ``MOTION_AFFINE``. Both images must be the same size. The refinement runs
    on a canvas capped at ECC_MAX_SIDE through levels [0.25, 0.5, 1.0], each
    ECC call capped at ``config.ecc_timeout_s`` wall-clock seconds and at most
    ``config.ecc_max_iterations`` iterations down to ``config.ecc_epsilon``.
    When ECC fails, times out or makes the chamfer RMS worse, the input matrix
    is returned with ``applied=False`` - never a worse transform than given.
    """
    criteria_count = config.ecc_max_iterations if config is not None else 200
    criteria_epsilon = config.ecc_epsilon if config is not None else 1e-6
    timeout_s = config.ecc_timeout_s if config is not None else 5.0
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT,
        criteria_count,
        criteria_epsilon,
    )
    motion = {
        TransformModel.SIMILARITY: cv2.MOTION_EUCLIDEAN,
        TransformModel.AFFINE: cv2.MOTION_AFFINE,
    }.get(model)
    if motion is None:
        raise ValueError(f"refine_ecc supports SIMILARITY and AFFINE, not {model.value!r}")

    old_gray = _as_gray(old_img)
    new_gray = _as_gray(new_img)
    rms_before = _ink_rms_px(old_gray, new_gray, np.asarray(initial, dtype=float))
    if old_gray.shape != new_gray.shape:
        # ECC needs same-size template and input; nothing to refine.
        return _no_change_result(initial, rms_before)

    full_height, full_width = old_gray.shape
    canvas = min(1.0, ECC_MAX_SIDE / max(full_height, full_width))
    old_canvas = cv2.resize(old_gray, None, fx=canvas, fy=canvas, interpolation=cv2.INTER_AREA)
    new_canvas = cv2.resize(new_gray, None, fx=canvas, fy=canvas, interpolation=cv2.INTER_AREA)

    # Work in canvas coordinates: the linear part is resolution-independent,
    # the translation scales with the canvas factor and again between pyramid
    # levels (level translation = canvas translation * level factor).
    matrix_canvas = np.asarray(initial, dtype=float).copy()
    matrix_canvas[0, 2] *= canvas
    matrix_canvas[1, 2] *= canvas
    correlation = float("nan")
    levels_run = 0
    timed_out = False
    for level in PYRAMID_LEVELS:
        if min(old_canvas.shape) * level < 12:
            break
        old_level = cv2.resize(old_canvas, None, fx=level, fy=level, interpolation=cv2.INTER_AREA)
        new_level = cv2.resize(new_canvas, None, fx=level, fy=level, interpolation=cv2.INTER_AREA)
        blurred_old = cv2.GaussianBlur(old_level, (0, 0), ECC_BLUR_SIGMA).astype(np.float32)
        blurred_new = cv2.GaussianBlur(new_level, (0, 0), ECC_BLUR_SIGMA).astype(np.float32)
        seed_canvas = matrix_canvas.copy()
        seed_canvas[0, 2] *= level
        seed_canvas[1, 2] *= level
        # ECC refines the template->input warp: seed and read back inverted.
        seed = cv2.invertAffineTransform(seed_canvas[:2].astype(np.float32))
        try:
            outcome = _ecc_with_timeout(blurred_new, blurred_old, seed, motion, criteria, timeout_s)
        except Exception:
            break  # ECC raised (e.g. "iterations do not converge")
        if outcome is None:
            timed_out = True
            break
        warp, correlation = outcome
        refined = _to_3x3(cv2.invertAffineTransform(warp))
        if not np.all(np.isfinite(refined)):
            break
        matrix_canvas = refined
        matrix_canvas[0, 2] /= level
        matrix_canvas[1, 2] /= level
        levels_run += 1

    candidate = matrix_canvas.copy()
    candidate[0, 2] /= canvas
    candidate[1, 2] /= canvas
    finite_candidate = np.all(np.isfinite(candidate))
    rms_candidate = _ink_rms_px(old_gray, new_gray, candidate) if finite_candidate else float("inf")
    completed = levels_run == len(PYRAMID_LEVELS) and not timed_out
    if finite_candidate and completed and math.isfinite(rms_candidate):
        improved = rms_candidate < rms_before - 1e-9
        if improved:
            return RefineResult(
                matrix=candidate,
                applied=True,
                iterations=levels_run,
                epsilon_final=float("nan"),
                correlation=correlation,
                rms_before_px=rms_before,
                rms_after_px=rms_candidate,
            )
    return _no_change_result(initial, rms_before, correlation, levels_run)
