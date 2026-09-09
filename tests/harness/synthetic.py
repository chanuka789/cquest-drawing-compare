"""Ground-truth synthetic test pair generator (Phase 4, Task 4.0).

Turn one real (or synthetic) PDF page into a test pair whose correct
alignment is known exactly: render the page once, then build the "new" image
by warping the grayscale old image with an analytically constructed
similarity. No fitting, no estimation: the ground-truth matrix is written
from the spec and the canvas geometry, and the warp applies that same
matrix, so the images and the recorded ground truth can never disagree.

Coordinate conventions, pinned here because every aligner and every test
depends on them:

* Images are 2-D grayscale uint8, row 0 at the top (OpenCV convention).
* The ground-truth matrix maps **old-image pixels onto new-image pixels**:
  ``p_new = M @ [x, y, 1]``, the same convention an alignment fitter returns.
* The similarity is applied about the old image centre ``(w/2, h/2)``, the
  translation spec is in millimetres and converted through ``px_per_mm`` in
  **old-image** pixel space, and a canvas resize scales everything about the
  origin afterwards: ``M = K @ R_similarity`` with ``K = diag(k, k, 1)`` and
  ``k = new_canvas_px / old_px``.
* ``rotation_deg`` is the linear-part angle ``atan2(m10, m00)`` of the
  ground-truth matrix: positive values rotate the content clockwise when the
  image is displayed with row 0 on top (equivalently, this is the angle a
  fitter on old->new correspondences reports). This is the convention
  ``similarity_parameters`` returns so spec values round-trip.
* ``cv2.warpAffine`` applies the supplied matrix as the forward content map
  (empirically: content landmark ``q`` lands at ``M q`` in the output), so
  ``generate_pair`` passes the ground-truth matrix straight through - no
  numeric inversion anywhere.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.utils.pdf_runtime import open_document

#: ISO A-series landscape paper in millimetres, (width, height).
ISO_LANDSCAPE_MM: dict[str, tuple[float, float]] = {
    "a4": (297.0, 210.0),
    "a3": (420.0, 297.0),
    "a2": (594.0, 420.0),
    "a1": (841.0, 594.0),
    "a0": (1189.0, 841.0),
}

#: How far inside each edge the sample grid of :meth:`SyntheticPair.error_of`
#: starts, and the spacing between grid points, in pixels.
_SAMPLE_STEP_PX = 50

_SHAPE_NAMES = {"rect", "circle", "line"}

_WHITE = 255

#: A callable that renders ``(source_pdf, page_index, dpi)`` to a grayscale
#: uint8 image at the same scale pypdfium2 would use for that dpi.
Renderer = Callable[[str | Path, int, int], NDArray[np.uint8]]

Rect = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class TransformSpec:
    """One known similarity applied between the two sheets.

    ``translation_mm`` shifts the drawing; ``rotation_deg`` and ``scale`` are
    applied about the old image centre; ``page_size_change`` first resizes
    the canvas to an ISO landscape size (mm), which multiplies the whole
    mapping by ``k = new_canvas_w_px / old_w_px``.
    """

    translation_mm: tuple[float, float] = (0.0, 0.0)
    rotation_deg: float = 0.0
    scale: float = 1.0
    page_size_change: str | None = None

    def __post_init__(self) -> None:
        if len(self.translation_mm) != 2:
            raise ValueError("translation_mm must be an (x, y) pair of millimetres")
        x, y = (float(v) for v in self.translation_mm)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"translation_mm must be finite, got {self.translation_mm!r}")
        object.__setattr__(self, "translation_mm", (x, y))
        rotation = float(self.rotation_deg)
        if not math.isfinite(rotation):
            raise ValueError(f"rotation_deg must be finite, got {self.rotation_deg!r}")
        object.__setattr__(self, "rotation_deg", rotation)
        scale = float(self.scale)
        if not math.isfinite(scale) or scale <= 0.0:
            raise ValueError(f"scale must be a positive finite number, got {self.scale!r}")
        object.__setattr__(self, "scale", scale)
        size = self.page_size_change
        if size is not None and size.lower() not in ISO_LANDSCAPE_MM:
            raise ValueError(
                f"page_size_change must be one of {sorted(ISO_LANDSCAPE_MM)!r} or None, "
                f"got {size!r}"
            )
        if size is not None:
            object.__setattr__(self, "page_size_change", size.lower())


@dataclass(frozen=True, slots=True)
class DegradationSpec:
    """Imperfections applied to the warped "new" image, in pipeline order.

    Content change (erase a rectangle in new-image pixels, optionally paste a
    filled cv2 shape by name) -> line weight -> skew -> blur -> noise -> JPEG.
    All values default to "no degradation".
    """

    #: Positive thickens strokes by n pixels (grayscale erosion of dark ink
    #: on white paper), negative thins them.
    line_weight_change: int = 0
    noise_sigma: float = 0.0
    jpeg_quality: int | None = None
    #: Horizontal shear angle in degrees, simulating a slightly skew scan.
    skew_deg: float = 0.0
    blur_sigma: float = 0.0
    #: Erase rectangle ``(x, y, w, h)`` in new-image pixels and optionally
    #: paste a shape by name: ``("rect" | "circle" | "line", ...)``.
    content_change: tuple[Rect | None, str | None] = (None, None)

    def __post_init__(self) -> None:
        if not isinstance(self.line_weight_change, int):
            raise ValueError(f"line_weight_change must be an int, got {self.line_weight_change!r}")
        noise = float(self.noise_sigma)
        if not math.isfinite(noise) or noise < 0.0:
            raise ValueError(f"noise_sigma must be a non-negative finite number, got {noise!r}")
        object.__setattr__(self, "noise_sigma", noise)
        quality = self.jpeg_quality
        if quality is not None and not (isinstance(quality, int) and 1 <= quality <= 100):
            raise ValueError(f"jpeg_quality must be an int in 1..100 or None, got {quality!r}")
        skew = float(self.skew_deg)
        if not math.isfinite(skew):
            raise ValueError(f"skew_deg must be finite, got {self.skew_deg!r}")
        object.__setattr__(self, "skew_deg", skew)
        blur = float(self.blur_sigma)
        if not math.isfinite(blur) or blur < 0.0:
            raise ValueError(f"blur_sigma must be a non-negative finite number, got {blur!r}")
        object.__setattr__(self, "blur_sigma", blur)
        rect, shape = self.content_change
        if rect is not None and (
            len(rect) != 4 or any(not isinstance(v, int) or v < 0 for v in rect)
        ):
            raise ValueError(f"content_change rectangle must be (x, y, w, h) >= 0, got {rect!r}")
        if shape is not None:
            if shape not in _SHAPE_NAMES:
                raise ValueError(
                    f"content_change shape must be one of {sorted(_SHAPE_NAMES)!r}, got {shape!r}"
                )
            if rect is None:
                raise ValueError("a content_change shape needs an erase rectangle to paste into")


@dataclass(slots=True)
class SyntheticPair:
    """One ground-truth pair: two images plus the exact transform between them."""

    old_image: NDArray[np.uint8]
    new_image: NDArray[np.uint8]
    #: 3x3 matrix mapping old-image pixels onto new-image pixels.
    gt_matrix: NDArray[np.float64]
    transform: TransformSpec
    degradation: DegradationSpec
    px_per_mm: float

    def error_of(self, computed_matrix: NDArray[np.float64]) -> float:
        """RMS error in px between a computed matrix and the ground truth.

        Samples a regular grid of points (every 50 px, inset 50 px from each
        edge, so the sample stays inside the drawing area), maps them with
        both matrices, and returns the RMS pixel distance.
        """
        matrix = as_affine(computed_matrix)
        height, width = self.old_image.shape[:2]
        x_max = width - _SAMPLE_STEP_PX
        y_max = height - _SAMPLE_STEP_PX
        if x_max <= _SAMPLE_STEP_PX or y_max <= _SAMPLE_STEP_PX:
            xs = np.linspace(0.0, float(max(width - 1, 0)), 9)
            ys = np.linspace(0.0, float(max(height - 1, 0)), 9)
        else:
            xs = np.arange(_SAMPLE_STEP_PX, x_max, _SAMPLE_STEP_PX, dtype=float)
            ys = np.arange(_SAMPLE_STEP_PX, y_max, _SAMPLE_STEP_PX, dtype=float)
        grid_x, grid_y = np.meshgrid(xs, ys)
        points = np.column_stack(
            [grid_x.ravel(), grid_y.ravel(), np.ones(grid_x.size, dtype=float)]
        )
        truth = points @ self.gt_matrix.T
        computed = points @ matrix.T
        deltas = truth[:, :2] - computed[:, :2]
        return float(np.sqrt(np.mean(np.sum(deltas * deltas, axis=1))))


def as_affine(matrix: NDArray[np.float64] | tuple[float, ...]) -> NDArray[np.float64]:
    """Return a 3x3 homogeneous matrix, padding a 2x3 affine if needed."""
    arr = np.asarray(matrix, dtype=float)
    if arr.shape == (3, 3):
        return arr
    if arr.shape == (2, 3):
        return np.vstack([arr, np.array([0.0, 0.0, 1.0])])
    raise ValueError(f"expected a 2x3 or 3x3 matrix, got shape {arr.shape}")


def similarity_parameters(matrix: NDArray[np.float64]) -> tuple[float, float, float, float]:
    """Decompose a similarity matrix into ``(tx_px, ty_px, rotation_deg, scale)``."""
    m = as_affine(matrix)
    scale = math.hypot(m[0, 0], m[1, 0])
    rotation_deg = math.degrees(math.atan2(m[1, 0], m[0, 0]))
    return float(m[0, 2]), float(m[1, 2]), rotation_deg, scale


def _render_page_gray(source_pdf: str | Path, page_index: int, dpi: int) -> NDArray[np.uint8]:
    """Render one page to grayscale uint8 through the guarded pdfium runtime."""
    with open_document(source_pdf) as document:
        bitmap = document[page_index].render(scale=dpi / 72.0, draw_annots=False)
        try:
            pixels = bitmap.to_numpy()  # copies the buffer; safe after bitmap.close()
        finally:
            bitmap.close()
    return cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)


def _ground_truth_matrix(
    transform: TransformSpec, px_per_mm: float, old_size: tuple[int, int], canvas_scale: float
) -> NDArray[np.float64]:
    """Build ``M = K @ R_similarity`` exactly, from cos/sin, no fitting."""
    theta = math.radians(transform.rotation_deg)
    scale = transform.scale
    centre_x = old_size[1] / 2.0
    centre_y = old_size[0] / 2.0
    cosine = math.cos(theta)
    sine = math.sin(theta)
    a = scale * cosine
    b = scale * sine
    # Similarity about the old centre, translation in old-image pixels:
    # p' = sR (p - c) + c + t, so the matrix translation is c - sR c + t.
    tx_px = transform.translation_mm[0] * px_per_mm
    ty_px = transform.translation_mm[1] * px_per_mm
    offset_x = centre_x - a * centre_x + b * centre_y + tx_px
    offset_y = centre_y - b * centre_x - a * centre_y + ty_px
    k = canvas_scale
    return np.array(
        [
            [k * a, -k * b, k * offset_x],
            [k * b, k * a, k * offset_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


def _new_canvas_pixels(
    transform: TransformSpec, old_size: tuple[int, int], px_per_mm: float
) -> tuple[tuple[int, int], float]:
    """Canvas pixel size for the new image and the uniform scale ``k``."""
    if transform.page_size_change is None:
        return (old_size[1], old_size[0]), 1.0
    width_mm, height_mm = ISO_LANDSCAPE_MM[transform.page_size_change]
    width_px = round(width_mm * px_per_mm)
    height_px = round(height_mm * px_per_mm)
    return (width_px, height_px), width_px / old_size[1]


def _apply_content_change(
    image: NDArray[np.uint8], rect: Rect | None, shape: str | None
) -> NDArray[np.uint8]:
    """Erase ``rect`` (white) and optionally paste a filled dark shape in it."""
    if rect is None:
        return image
    height, width = image.shape[:2]
    x, y, w, h = rect
    x0 = min(max(x, 0), width)
    y0 = min(max(y, 0), height)
    x1 = min(max(x + w, 0), width)
    y1 = min(max(y + h, 0), height)
    if x1 <= x0 or y1 <= y0:
        return image
    image[y0:y1, x0:x1] = _WHITE
    if shape is None:
        return image
    region_w = x1 - x0
    region_h = y1 - y0
    pad = min(region_w, region_h) // 10
    centre_x = (x0 + x1) // 2
    centre_y = (y0 + y1) // 2
    if shape == "rect":
        cv2.rectangle(
            image, (x0 + pad, y0 + pad), (x1 - 1 - pad, y1 - 1 - pad), 0, thickness=cv2.FILLED
        )
    elif shape == "circle":
        radius = max(1, min(region_w, region_h) // 3)
        cv2.circle(image, (centre_x, centre_y), radius, 0, thickness=cv2.FILLED)
    else:  # line: a bold diagonal, the kind of mark a designer scribbles
        thickness = max(2, min(region_w, region_h) // 12)
        start = (x0 + region_w * 2 // 10, y0 + region_h * 8 // 10)
        end = (x0 + region_w * 8 // 10, y0 + region_h * 2 // 10)
        cv2.line(image, start, end, 0, thickness, lineType=cv2.LINE_AA)
    return image


def _apply_degradation(image: NDArray[np.uint8], spec: DegradationSpec) -> NDArray[np.uint8]:
    """Apply a degradation spec to the warped new image, in pipeline order."""
    out = image.copy()
    rect, shape = spec.content_change
    _apply_content_change(out, rect, shape)
    if spec.line_weight_change:
        radius = abs(spec.line_weight_change)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        # Dark strokes on white: erode thickens them, dilate thins them.
        out = cv2.erode(out, kernel) if spec.line_weight_change > 0 else cv2.dilate(out, kernel)
    if spec.skew_deg:
        shear = math.tan(math.radians(spec.skew_deg))
        height = out.shape[0]
        # Content map x' = x + shear * (y - cy); warpAffine applies it forward.
        content_map = np.array([[1.0, shear, -shear * height / 2.0], [0.0, 1.0, 0.0]], dtype=float)
        out = cv2.warpAffine(out, content_map, (out.shape[1], height), borderValue=_WHITE)
    if spec.blur_sigma:
        out = cv2.GaussianBlur(out, (0, 0), sigmaX=spec.blur_sigma)
    if spec.noise_sigma:
        rng = np.random.default_rng()
        noise = rng.normal(0.0, spec.noise_sigma, size=out.shape)
        out = np.clip(out.astype(np.float32) + noise, 0.0, 255.0).astype(np.uint8)
    if spec.jpeg_quality is not None:
        ok, buffer = cv2.imencode(
            ".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), int(spec.jpeg_quality)]
        )
        if not ok:
            raise RuntimeError("cv2 failed to encode the JPEG degradation")
        out = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    return out


def generate_pair(
    source_pdf: str | Path,
    page: int = 0,
    *,
    transform: TransformSpec,
    degradation: DegradationSpec | None = None,
    dpi: int = 100,
    renderer: Renderer | None = None,
) -> SyntheticPair:
    """Render ``source_pdf`` page ``page`` and warp it into a ground-truth pair.

    The old image is the page rendered at ``dpi``. The new image is the old
    one warped with the *analytic* ground-truth matrix (never fitted) onto a
    canvas of the target ISO size when ``page_size_change`` is set, filled
    with white outside the mapped content, then degraded if requested.

    ``renderer`` overrides the internal pypdfium2 rasteriser; it must be a
    callable ``(source_pdf, page_index, dpi) -> grayscale uint8 image`` whose
    pixel size matches pypdfium2's ``dpi/72`` scale, because the canvas math
    assumes ``px_per_mm = dpi / 25.4``.
    """
    spec = degradation if degradation is not None else DegradationSpec()
    px_per_mm = dpi / 25.4
    if renderer is None:
        old_image = _render_page_gray(source_pdf, page, dpi)
    elif callable(renderer):
        old_image = np.asarray(renderer(source_pdf, page, dpi), dtype=np.uint8)
        if old_image.ndim != 2:
            raise ValueError(
                f"custom renderer must return a 2-D grayscale image, got {old_image.shape}"
            )
    else:
        raise TypeError(f"renderer must be a callable or None, got {type(renderer).__name__}")
    old_size = old_image.shape[:2]
    canvas_size, canvas_scale = _new_canvas_pixels(transform, old_size, px_per_mm)
    gt_matrix = _ground_truth_matrix(transform, px_per_mm, old_size, canvas_scale)
    new_image = cv2.warpAffine(
        old_image, gt_matrix[:2], canvas_size, flags=cv2.INTER_LINEAR, borderValue=_WHITE
    )
    new_image = _apply_degradation(new_image, spec)
    return SyntheticPair(
        old_image=old_image,
        new_image=new_image,
        gt_matrix=gt_matrix,
        transform=transform,
        degradation=spec,
        px_per_mm=px_per_mm,
    )
