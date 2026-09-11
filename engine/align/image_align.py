"""Image-based alignment fallbacks (plan B4 methods 3-5, task 4.7).

Three methods, all returning a :class:`TransformResult` whose 3x3 matrix maps
old-sheet pixels onto the new sheet (``p_new = M @ [x, y, 1]``), consistent
with ``TransformResult.apply``:

* :func:`align_phase_correlation` - log-polar phase correlation of the FFT
  magnitude spectra (method 3). Global, needs no features, so it is the
  strongest fallback for scanned line drawings.
* :func:`align_features` - SIFT/ORB keypoints, FLANN matching with Lowe's
  ratio test, RANSAC similarity fit (method 4). Honest caveat: line drawings
  are a hard case for feature detectors; this is a fallback below phase
  correlation.
* :func:`align_sheet_border` - the outer drawing-frame rectangle, corner
  correspondence (method 5). This aligns the *sheet*, not the content, and is
  always low confidence.

``cv2.logPolar`` was removed in OpenCV 5.0; this module uses
``cv2.warpPolar`` with ``WARP_POLAR_LOG`` instead. Its output rows are the
angle axis (360 degrees across the output height) and its columns are the
log-radius axis.

Empirically calibrated conversion factors (probed and validated against
synthetic ground-truth pairs; the calibration is described in the tests):

* Rotation: a content rotation that appears as ``atan2(m10, m00)`` of the
  final matrix shifts the log-polar rows by ``dy`` with
  ``rotation_deg = -dy * 360 / lp_rows``.
* Scale: a content scale ``s`` (old -> new) shifts the log-polar columns by
  ``dx`` with ``ln(s) = dx * ln(max_radius) / (lp_cols - 13)``. The -13 term
  is empirical: cv2's log remap maps radius 1..max_radius onto only the first
  ``lp_cols - 13`` output columns (probed at several canvas sizes, OpenCV
  5.0.0). Without it the scale estimate is systematically ~2 % low.

Accuracy notes (measured on synthetic line art at ~800x600, 25 % coarse, see
``tests/test_image_align.py``): rotation within ~0.3 deg, scale within ~1 %,
translation within ~1 px. Rotations are only searched in the range
(-45, +45) degrees; 90-degree page rotations must be normalised upstream
(the PDF /Rotate flag is applied before rendering).
"""

from __future__ import annotations

import math

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.types import AlignConfig, AlignMethod, TransformModel, TransformResult

#: Output size of the log-polar remap. Rows cover the full 360 degrees of
#: the magnitude spectrum; columns cover log radius.
LP_ROWS = 512
LP_COLS = 512
#: Empirically the log-radius axis spans only the first LP_COLS - 13 output
#: columns (OpenCV 5.0.0 warpPolar WARP_POLAR_LOG); measured 6 canvases.
LOG_COLS_EFFECTIVE = LP_COLS - 13
#: Gaussian blur sigma applied before the FFT (smooths resampling noise that
#: otherwise decorrelates thin-line spectra, see module docstring of refine).
SPEC_BLUR_SIGMA = 1.0
#: Row band of the log-polar image that carries usable rotation/scale signal
#: (fraction of the log-radius columns). The DC blob lives at the innermost
#: columns; the outermost columns are border fill.
BAND_LO_FRACTION = 0.06
BAND_HI_FRACTION = 0.94
#: Working canvas: images are rescaled so the smaller side lands in
#: [WORKING_MIN_SIDE, WORKING_MAX_SIDE] before the correlation stages. A
#: minimum size keeps rotation/scale estimates stable (discrete resampling of
#: thin lines decorrelates spectra at very small canvases); a maximum caps
#: FFT cost.
WORKING_MIN_SIDE = 320.0
WORKING_MAX_SIDE = 1024.0
#: Rotation search half-range, degrees (rows searched around zero).
ROT_SEARCH_HALF_DEG = 45.0


def _grayscale(img: np.ndarray) -> NDArray[np.float32]:
    """Convert an image to float32 grayscale with values in [0, 255]."""
    arr = np.asarray(img)
    if arr.ndim == 2:
        gray = arr
    elif arr.ndim == 3 and arr.shape[2] == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
    else:
        raise ValueError(f"unsupported image shape {arr.shape}")
    return gray.astype(np.float32)


def _downsample(img: np.ndarray, factor: float) -> tuple[np.ndarray, float]:
    """Resize *img* by *factor* (INTER_AREA) and return (small, factor).

    The dtype is preserved (float32 grayscale for the FFT pipeline, uint8 for
    the feature detectors). The returned factor is the actual per-pixel scale
    of the resize (rounding may make it differ from the requested one by a
    fraction of a pixel).
    """
    height, width = img.shape[:2]
    small_w = max(1, round(width * factor))
    small_h = max(1, round(height * factor))
    small = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_AREA)
    actual = (small_h / height + small_w / width) / 2.0
    return small, actual


def _hanning2d(shape: tuple[int, int]) -> NDArray[np.float32]:
    """2D Hanning window of shape (height, width), values in [0, 1]."""
    height, width = shape
    return cv2.createHanningWindow((width, height), cv2.CV_32F)


def _magnitude_spectrum(gray: np.ndarray) -> NDArray[np.float32]:
    """Log-magnitude spectrum: blur, subtract mean, FFT, shift, log1p."""
    blurred = cv2.GaussianBlur(gray, (0, 0), SPEC_BLUR_SIGMA).astype(np.float32)
    centered = blurred - blurred.mean()
    spectrum = np.fft.fftshift(np.fft.fft2(centered))
    return np.log1p(np.abs(spectrum)).astype(np.float32)


def _log_polar(mag: np.ndarray) -> tuple[NDArray[np.float32], float]:
    """Semi-log polar remap of a magnitude spectrum (rows = angle).

    Returns (log_polar, max_radius): max_radius is min(shape)/2, the radius
    the outermost log-radius column samples.
    """
    height, width = mag.shape
    max_radius = min(height, width) / 2.0
    lp = cv2.warpPolar(
        mag,
        (LP_ROWS, LP_COLS),
        (width / 2.0, height / 2.0),
        max_radius,
        cv2.WARP_POLAR_LOG | cv2.INTER_LINEAR | cv2.WARP_FILL_OUTLIERS,
    )
    return lp.astype(np.float32), max_radius


def _band_rows_removed(lp: np.ndarray) -> NDArray[np.float32]:
    """Keep the signal band and remove the per-column (radial) mean.

    The radial amplitude profile is rotation invariant and would otherwise
    swamp the angular signal that carries the rotation; removing each column's
    mean over the angle axis leaves the angular structure.
    """
    band = np.zeros_like(lp)
    lo = int(LP_COLS * BAND_LO_FRACTION)
    hi = int(LP_COLS * BAND_HI_FRACTION)
    band[:, lo:hi] = lp[:, lo:hi]
    band[:, lo:hi] -= band[:, lo:hi].mean(axis=0, keepdims=True)
    return band


def _parabolic_peak(values: dict[int, float]) -> tuple[float, float]:
    """Subpixel parabolic refinement of a 1D correlation dictionary."""
    best = max(values, key=values.get)
    center = values[best]
    left = values.get(best - 1, center)
    right = values.get(best + 1, center)
    denom = left - 2.0 * center + right
    if abs(denom) < 1e-12:
        return float(best), center
    refined = best + 0.5 * (left - right) / denom
    return refined, center


def _phase_correlation_surface(a: np.ndarray, b: np.ndarray) -> NDArray[np.float64]:
    """Whitened (phase-only) circular correlation surface of two arrays."""
    fa = np.fft.fft2(a)
    fb = np.fft.fft2(b)
    cross = fa * np.conj(fb)
    eps = 1e-8 * (float(np.abs(fa).mean()) * float(np.abs(fb).mean()) + 1e-12)
    surface = np.fft.ifft2(cross / (np.abs(cross) + eps)).real
    return np.fft.fftshift(surface)


def _rotation_residual(lp_a: np.ndarray, lp_b: np.ndarray) -> tuple[float, float]:
    """Residual rotation (deg) of lp_b relative to lp_a, plus peak response.

    Correlates the angle-marginal (sum over log-radius after removing the
    radial profile) circularly over rows and refines the peak parabolically.
    Positive degrees follow the transform convention ``atan2(m10, m00)``.
    """
    a = _band_rows_removed(lp_a)
    b = _band_rows_removed(lp_b)
    marginal_a = a.sum(axis=1)
    marginal_b = b.sum(axis=1)
    denom = float(np.linalg.norm(marginal_a) * np.linalg.norm(marginal_b))
    if denom <= 0.0:
        return 0.0, 0.0
    half_rows = round(LP_ROWS * ROT_SEARCH_HALF_DEG / 360.0)
    values = {
        shift: float(np.dot(marginal_a, np.roll(marginal_b, shift))) / denom
        for shift in range(-half_rows, half_rows + 1)
    }
    dy, response = _parabolic_peak(values)
    return -dy * 360.0 / LP_ROWS, response


def _scale_residual(lp_a: np.ndarray, lp_b: np.ndarray) -> tuple[float, float]:
    """Residual log-radius column shift of lp_b vs lp_a, plus peak response.

    Whitened phase correlation on the banded log-polar images with the row
    search restricted to a few rows (rotation must already be removed, see
    :func:`align_phase_correlation`); the column peak is the scale signal.
    """
    a = _band_rows_removed(lp_a)
    b = _band_rows_removed(lp_b)
    surface = _phase_correlation_surface(a, b)
    center = LP_ROWS // 2
    half = 6
    sub = surface[center - half : center + half + 1, :]
    peak = np.unravel_index(np.argmax(sub), sub.shape)
    dx = peak[1] - LP_COLS // 2
    return float(dx), float(sub[peak])


def _similarity_about_center(
    width: float,
    height: float,
    rotation_deg: float,
    scale: float,
    tx: float = 0.0,
    ty: float = 0.0,
) -> NDArray[np.float64]:
    """3x3 similarity (rotate ``rotation_deg``, scale, then translate).

    ``rotation_deg`` follows the transform convention used by
    :func:`_decompose` (``atan2(m10, m00)`` of the linear part).
    """
    radians = math.radians(rotation_deg)
    cx = width / 2.0
    cy = height / 2.0
    matrix = np.eye(3, dtype=float)
    matrix[0, 0] = scale * math.cos(radians)
    matrix[0, 1] = -scale * math.sin(radians)
    matrix[1, 0] = scale * math.sin(radians)
    matrix[1, 1] = scale * math.cos(radians)
    matrix[0, 2] = cx - matrix[0, 0] * cx - matrix[0, 1] * cy + tx
    matrix[1, 2] = cy - matrix[1, 0] * cx - matrix[1, 1] * cy + ty
    return matrix


def _warp_white(img: np.ndarray, matrix: np.ndarray) -> NDArray[np.float32]:
    """Render *img* mapped by a 3x3 similarity onto a white canvas."""
    height, width = img.shape[:2]
    warped = cv2.warpAffine(
        img,
        matrix[:2].astype(np.float64),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    return warped.astype(np.float32)


def _decompose(matrix: np.ndarray) -> dict[str, float]:
    """Decompose a 3x3 matrix into similarity parameters for reporting.

    ``rotation_deg`` is ``atan2(m10, m00)``; ``scale`` is the norm of the
    first column; ``shear`` is the normalised column dot product (zero for a
    pure similarity); the other keys mirror ``TransformResult`` fields.
    """
    a, b, c, d = matrix[0, 0], matrix[0, 1], matrix[1, 0], matrix[1, 1]
    sx = math.hypot(a, c)
    sy = math.hypot(b, d)
    denom = max(sx * sy, 1e-12)
    return {
        "scale": (sx + sy) / 2.0,
        "rotation_deg": math.degrees(math.atan2(c, a)),
        "shear": (a * b + c * d) / denom,
        "determinant": a * d - b * c,
        "tx_px": float(matrix[0, 2]),
        "ty_px": float(matrix[1, 2]),
    }


def _failed_result(method: AlignMethod, note: str) -> TransformResult:
    """A TransformResult that reports failure (rms=inf, identity matrix)."""
    result = TransformResult(method=method, model=TransformModel.SIMILARITY, note=note)
    return result


def _result_from_matrix(
    matrix: np.ndarray,
    method: AlignMethod,
    confidence: float,
    note: str,
) -> TransformResult:
    """A TransformResult built from a fitted matrix and decomposed fields."""
    parts = _decompose(matrix)
    return TransformResult(
        matrix=np.asarray(matrix, dtype=float),
        model=TransformModel.SIMILARITY,
        method=method,
        rms_residual_px=0.0,
        confidence=min(1.0, confidence),
        scale=parts["scale"],
        rotation_deg=parts["rotation_deg"],
        shear=parts["shear"],
        determinant=parts["determinant"],
        tx_px=parts["tx_px"],
        ty_px=parts["ty_px"],
        note=note,
    )


def _rescale_to_working(gray: np.ndarray) -> NDArray[np.float32]:
    """Rescale so the smaller side lands in [WORKING_MIN_SIDE, WORKING_MAX_SIDE]."""
    height, width = gray.shape
    small = min(height, width)
    if small < WORKING_MIN_SIDE:
        factor = WORKING_MIN_SIDE / small
        interpolation = cv2.INTER_LINEAR
    elif small > WORKING_MAX_SIDE:
        factor = WORKING_MAX_SIDE / small
        interpolation = cv2.INTER_AREA
    else:
        return gray.astype(np.float32)
    new_w = max(1, round(width * factor))
    new_h = max(1, round(height * factor))
    return cv2.resize(gray, (new_w, new_h), interpolation=interpolation).astype(np.float32)


def align_phase_correlation(
    old_img: np.ndarray,
    new_img: np.ndarray,
    *,
    coarse: float = 0.25,
    config: AlignConfig | None = None,
) -> TransformResult:
    """Log-polar phase-correlation alignment (plan B4 method 3).

    1. Downscale both images by ``coarse`` (INTER_AREA).
    2. Work on a common canvas (smaller side in [320, 1024] px) so resampling
       noise does not decorrelate thin-line spectra.
    3. Estimate rotation from the angle marginal of the log-polar magnitude
       spectra (circular correlation, range +/-45 deg).
    4. Correct the old image by that rotation in image space, then estimate
       the scale from the log-radius column shift of the phase-correlation
       surface (whitened, row-restricted). Iterating rotation -> scale on the
       corrected image converges because each residual is small.
    5. Phase-correlate the corrected old image against the new one
       (Hanning window, Gaussian pre-blur) to recover the translation.
    6. **90-degree candidate search.** Periodic content (grids, hatch) makes a
       +/-45 deg rotation estimate ambiguous under multiples of 90 degrees.
       The whole estimate therefore runs with the old sheet pre-rotated by
       each of 0/90/180/270 degrees; the candidate with the strongest
       correlation response wins and its transform is composed with the
       pre-rotation. Without this, a 90-degree rotated drawing can be
       'confidently' aligned the wrong way — the exact failure the quality
       gate exists to prevent.
    7. Scale the matrix back to the full-resolution image and decompose it.

    The returned matrix maps old pixels onto new pixels. ``confidence`` is
    the strongest correlation response of the three stages. On failure (size
    mismatch, non-finite estimate, degenerate input) the returned
    ``TransformResult`` carries the method and ``rms_residual_px = inf``.
    """
    note = "log-polar phase correlation of FFT magnitudes"
    try:
        old_gray = _grayscale(old_img)
        new_gray = _grayscale(new_img)
        if old_gray.shape != new_gray.shape:
            return _failed_result(
                AlignMethod.PHASE_CORRELATION,
                "phase correlation needs same-size sheets, got "
                f"{old_gray.shape} vs {new_gray.shape}",
            )

        def estimate(candidate_old: np.ndarray, steps: int) -> tuple[np.ndarray, float] | None:
            """Run the rotation/scale/translation estimate for one candidate.

            Returns the full-resolution matrix mapping *candidate_old* pixels
            onto *new* pixels, plus the correlation confidence, or None.
            """
            full_width, full_height = candidate_old.shape[1], candidate_old.shape[0]
            old_small, _ = _downsample(candidate_old, coarse)
            new_small, _ = _downsample(new_gray, coarse)
            old_work = _rescale_to_working(old_small)
            new_work = _rescale_to_working(new_small)
            if old_work.shape != new_work.shape:
                return None
            work_height, work_width = old_work.shape
            lp_new, max_radius = _log_polar(_magnitude_spectrum(new_work))
            min_scale = config.min_scale if config is not None else 0.2
            max_scale = config.max_scale if config is not None else 5.0

            matrix = np.eye(3, dtype=float)
            responses: list[float] = []
            converged = False
            for _ in range(6):
                corrected = _warp_white(old_work, matrix)
                lp_corrected, _ = _log_polar(_magnitude_spectrum(corrected))
                rotation, rot_response = _rotation_residual(lp_corrected, lp_new)
                responses.append(rot_response)
                dx, scale_response = _scale_residual(lp_corrected, lp_new)
                responses.append(scale_response)
                if abs(rotation) < 0.02 and abs(dx) <= 0.6:
                    converged = True
                    break
                scale = math.exp(dx * math.log(max_radius) / LOG_COLS_EFFECTIVE)
                if not math.isfinite(scale) or not min_scale <= scale <= max_scale:
                    return None
                matrix = _similarity_about_center(work_width, work_height, rotation, scale) @ matrix
            if not converged:
                return None

            corrected = _warp_white(old_work, matrix)
            window = _hanning2d(corrected.shape)
            smooth_old = cv2.GaussianBlur(corrected, (0, 0), SPEC_BLUR_SIGMA)
            smooth_new = cv2.GaussianBlur(new_work, (0, 0), SPEC_BLUR_SIGMA)
            (tx, ty), translation_response = cv2.phaseCorrelate(
                smooth_old * window, smooth_new * window
            )
            responses.append(float(translation_response))
            if not all(math.isfinite(v) for v in (tx, ty)):
                return None
            matrix = _similarity_about_center(0.0, 0.0, 0.0, 1.0, tx, ty) @ matrix
            # Convert from working-canvas pixels back to the candidate's size.
            matrix[0, 2] *= full_width / work_width
            matrix[1, 2] *= full_height / work_height
            if not np.all(np.isfinite(matrix)):
                return None
            return matrix, max(responses, default=0.0)

        best_matrix: np.ndarray | None = None
        best_confidence = 0.0
        best_steps = 0
        height, width = old_gray.shape
        centre = ((width - 1) / 2.0, (height - 1) / 2.0)
        for steps in range(4):
            rotation2x3 = cv2.getRotationMatrix2D(centre, 90.0 * steps, 1.0)
            pre_rotation = np.eye(3, dtype=float)
            pre_rotation[:2, :] = rotation2x3
            candidate_old = cv2.warpAffine(
                old_gray,
                rotation2x3,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderValue=255,
            )
            estimate_matrix = estimate(candidate_old, steps)
            if estimate_matrix is None:
                continue
            matrix_candidate, confidence = estimate_matrix
            # candidate = pre_rotation . old, so old -> new is
            # (candidate -> new) . pre_rotation.
            full_matrix = matrix_candidate @ pre_rotation
            if np.all(np.isfinite(full_matrix)) and confidence > best_confidence:
                best_matrix, best_confidence, best_steps = (
                    full_matrix,
                    confidence,
                    steps,
                )
        if best_matrix is None:
            return _failed_result(
                AlignMethod.PHASE_CORRELATION,
                "rotation/scale/translation estimation failed on all 90-degree candidates",
            )
        detail = note + f"; peak response {best_confidence:.2f}"
        if best_steps:
            detail += f"; resolved {best_steps * 90} degree pre-rotation"
        return _result_from_matrix(
            best_matrix, AlignMethod.PHASE_CORRELATION, best_confidence, detail
        )
    except (cv2.error, ValueError, FloatingPointError) as exc:
        return _failed_result(AlignMethod.PHASE_CORRELATION, f"failed: {exc}")


def _detect_and_match(
    old_small: np.ndarray,
    new_small: np.ndarray,
    detector: str,
) -> tuple[list[cv2.KeyPoint], list[cv2.KeyPoint], list[cv2.DMatch]] | None:
    """Detect keypoints and apply knn + Lowe ratio test; None when impossible."""
    if detector == "orb":
        orb = cv2.ORB_create(nfeatures=4000)
        kp_old, desc_old = orb.detectAndCompute(old_small, None)
        kp_new, desc_new = orb.detectAndCompute(new_small, None)
        if desc_old is None or desc_new is None:
            return None
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    elif detector == "sift":
        sift = cv2.SIFT_create(contrastThreshold=0.05, edgeThreshold=15)
        kp_old, desc_old = sift.detectAndCompute(old_small, None)
        kp_new, desc_new = sift.detectAndCompute(new_small, None)
        if desc_old is None or desc_new is None:
            return None
        matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {})
    else:
        raise ValueError(f"unknown detector {detector!r}, use 'sift' or 'orb'")
    if len(kp_old) < 4 or len(kp_new) < 4:
        return None
    matches = matcher.knnMatch(desc_old, desc_new, k=2)
    good = [
        m
        for pair in matches
        if len(pair) == 2
        for m, neighbour in [pair]
        if m.distance < 0.75 * neighbour.distance
    ]
    if len(good) < 4:
        return None
    return kp_old, kp_new, good


def align_features(
    old_img: np.ndarray,
    new_img: np.ndarray,
    *,
    detector: str = "sift",
    config: AlignConfig | None = None,
) -> TransformResult:
    """Feature-based alignment (plan B4 method 4) - a fallback.

    Honest caveat: line drawings are a hard case for feature detectors - few
    distinctive corners, heavy repetition, large white areas. This method is
    expected to underperform phase correlation on drawings, which is why the
    cascade tries it afterwards. Realistic CAD/scan content usually carries a
    few distinctive marks (title block text, dense hatch corners) that still
    yield enough SIFT anchors; synthetic line art without such marks does not.

    Detection happens on images downsampled by ``config.coarse_scale``
    (default 0.25). SIFT uses a raised contrast threshold to suppress noise
    keypoints; matches use FLANN (or Hamming BFMatcher for ORB) with Lowe's
    ratio test 0.75; the fit is a RANSAC similarity
    (``cv2.estimateAffinePartial2D``, which refits on the inlier set) with the
    inlier threshold from ``config.ransac_inlier_threshold_px``. The returned
    ``TransformResult`` carries inlier_count / inlier_ratio and
    ``rms_residual_px`` of the inliers, scaled to the full-resolution image.
    """
    note = "SIFT/ORB feature matching with RANSAC similarity fit (fallback method)"
    coarse_value = config.coarse_scale if config is not None else 0.25
    min_anchors = config.min_anchors if config is not None else 3
    try:
        old_gray = _grayscale(old_img)
        new_gray = _grayscale(new_img)
        full_height, full_width = old_gray.shape
        old_u8 = np.clip(old_gray, 0, 255).astype(np.uint8)
        new_u8 = np.clip(new_gray, 0, 255).astype(np.uint8)
        old_small, _ = _downsample(old_u8, coarse_value)
        new_small, _ = _downsample(new_u8, coarse_value)
        coarse_x = old_small.shape[1] / full_width
        coarse_y = old_small.shape[0] / full_height
        detected = _detect_and_match(old_small, new_small, detector)
        if detected is None:
            return _failed_result(
                AlignMethod.FEATURES,
                f"{detector}: too few keypoints or ratio-test matches on line art",
            )
        kp_old, kp_new, good = detected
        src = np.float32([kp_old[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp_new[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        threshold = config.ransac_inlier_threshold_px if config is not None else 3.0
        iterations = config.ransac_iterations if config is not None else 2000
        affine, mask = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=threshold,
            maxIters=iterations,
        )
        if mask is None:
            return _failed_result(AlignMethod.FEATURES, "RANSAC returned no inlier mask")
        inlier_mask = mask.ravel().astype(bool)
        inlier_count = int(inlier_mask.sum())
        inlier_ratio = inlier_count / len(good)
        if inlier_count < min_anchors:
            return _failed_result(
                AlignMethod.FEATURES,
                f"only {inlier_count} RANSAC inliers (need >= {min_anchors})",
            )
        src_in = src[inlier_mask].reshape(-1, 2)
        dst_in = dst[inlier_mask].reshape(-1, 2)
        warped_in = src_in @ affine[:, :2].T + affine[:, 2]
        residual = float(np.sqrt(np.mean(np.sum((warped_in - dst_in) ** 2, axis=1))))
        matrix = np.eye(3, dtype=float)
        matrix[:2, :] = affine
        matrix[0, 2] /= coarse_x
        matrix[1, 2] /= coarse_y
        parts = _decompose(matrix)
        return TransformResult(
            matrix=matrix,
            model=TransformModel.SIMILARITY,
            method=AlignMethod.FEATURES,
            inlier_ratio=inlier_ratio,
            rms_residual_px=residual / min(coarse_x, coarse_y),
            inlier_count=inlier_count,
            correspondence_count=len(good),
            confidence=min(1.0, inlier_ratio),
            scale=parts["scale"],
            rotation_deg=parts["rotation_deg"],
            shear=parts["shear"],
            determinant=parts["determinant"],
            tx_px=parts["tx_px"],
            ty_px=parts["ty_px"],
            note=note + f" ({detector}, {inlier_count} inliers)",
        )
    except (cv2.error, ValueError, FloatingPointError) as exc:
        return _failed_result(AlignMethod.FEATURES, f"failed: {exc}")


def _sorted_corners(quadrilateral: np.ndarray) -> NDArray[np.float32]:
    """Order four polygon points counter-clockwise about their centroid."""
    points = quadrilateral.reshape(-1, 2).astype(np.float32)
    cx, cy = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - cy, points[:, 0] - cx)
    return points[np.argsort(angles)]


def _sheet_corners(gray: np.ndarray) -> NDArray[np.float32] | None:
    """Detect the outer drawing frame: corners of the largest contour.

    Otsu thresholding needs an 8-bit image, so float grayscale is cast first.
    """
    u8 = np.clip(gray, 0, 255).astype(np.uint8)
    binary = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    contours = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(largest, True)
    if perimeter <= 0.0:
        return None
    polygon = cv2.approxPolyDP(largest, 0.02 * perimeter, True)
    if len(polygon) != 4:
        return None
    return _sorted_corners(polygon)


def align_sheet_border(old_img: np.ndarray, new_img: np.ndarray) -> TransformResult:
    """Align the outer sheet-frame rectangle (plan B4 method 5).

    Thresholds both sheets (Otsu), finds the largest external contour, fits a
    quadrilateral (approxPolyDP at 2 % of the perimeter) and matches its four
    corners with a least-squares similarity fit.

    Warning: this aligns the sheet *frame*, not the drawing content. When the
    content moved within a stationary frame this gives exactly the wrong
    answer, so the method always returns low confidence (0.4) with a note, and
    the orchestrator should only use it as a coarse starting point or a last
    resort (the plan says exactly that).
    """
    note = "aligns the sheet frame, not the content; low confidence by design"
    try:
        old_gray = _grayscale(old_img)
        new_gray = _grayscale(new_img)
        old_corners = _sheet_corners(old_gray)
        new_corners = _sheet_corners(new_gray)
        if old_corners is None or new_corners is None:
            return _failed_result(
                AlignMethod.SHEET_BORDER,
                "could not find a 4-corner sheet frame on both images",
            )
        affine, _ = cv2.estimateAffinePartial2D(old_corners, new_corners)
        matrix = np.eye(3, dtype=float)
        matrix[:2, :] = affine
        if not np.all(np.isfinite(matrix)):
            return _failed_result(AlignMethod.SHEET_BORDER, "corner fit is not finite")
        warped = old_corners @ affine[:, :2].T + affine[:, 2]
        residual = float(np.sqrt(np.mean(np.sum((warped - new_corners) ** 2, axis=1))))
        parts = _decompose(matrix)
        return TransformResult(
            matrix=matrix,
            model=TransformModel.SIMILARITY,
            method=AlignMethod.SHEET_BORDER,
            inlier_ratio=1.0,
            rms_residual_px=residual,
            inlier_count=4,
            correspondence_count=4,
            confidence=0.4,
            scale=parts["scale"],
            rotation_deg=parts["rotation_deg"],
            shear=parts["shear"],
            determinant=parts["determinant"],
            tx_px=parts["tx_px"],
            ty_px=parts["ty_px"],
            note=note,
        )
    except (cv2.error, ValueError, FloatingPointError) as exc:
        return _failed_result(AlignMethod.SHEET_BORDER, f"failed: {exc}")
