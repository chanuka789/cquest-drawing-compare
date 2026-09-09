"""Render PDF pages to numpy arrays with pypdfium2.

Empirical findings from the pypdfium2 5.13.0 probes this module was built
against (pinned by the tests in ``tests/test_raster_renderer.py``):

* **Pixel layout.** ``PdfBitmap.to_numpy()`` on a default ``render()`` is BGR:
  an all-red page reads ``[0, 0, 255]``. Passing ``rev_byteorder=True`` makes
  pdfium write the bytes in RGB order directly, so this module renders with
  reverse byte order and the returned colour array is contiguous RGB without
  a full-buffer colour conversion. Grayscale is derived with
  ``cv2.cvtColor(..., COLOR_RGB2GRAY)``.
* **Page rotation.** pdfium applies the page's own ``/Rotate`` flag while
  rendering, and ``PdfPage.get_size()`` reports the rotation-adjusted
  dimensions. Rendering a ``/Rotate=90`` page with ``rotation=0`` produces
  exactly the same bitmap as rendering the unrotated page with
  ``rotation=90`` (bitwise, including rotated text). This module therefore
  always renders upright with ``rotation=0`` and records the page's flag as
  ``pdf_rotation`` with ``applied_rotation=0``.
* **Crop / banding.** ``PdfPage.render()``'s ``crop`` argument is *not* a
  region: it is the amount to cut off each side ``(left, bottom, right,
  top)`` in PDF canvas units, quantised to whole pixels with ``ceil``.
  Rendering a horizontal strip this way is **not** bitwise identical to a
  full render — anti-aliased pixels along non-axis-aligned strokes differ
  deterministically from the full-page rasterisation — and raw
  ``FPDF_RenderPageBitmap`` offsets behave the same. Banded re-rasterisation
  is therefore impossible if outputs must match a full render exactly.
  Instead this module renders the full page once, at the full scale, into a
  single preallocated numpy canvas (via the ``bitmap_maker`` parameter, so
  pdfium never holds a second full-page buffer), and every derived operation
  (grayscale, annotation diff) is applied in horizontal bands of a bounded
  row height. The canvas is allocated once and filled in place; the numpy
  working set never exceeds the returned arrays plus one band.

Memory: the full-page size is estimated as ``width_px * height_px * 4``
(pdfium's native BGRA sizing) before anything is allocated. When that
estimate exceeds ``RenderOptions.memory_budget_mb`` the render is flagged
``rendered_in_bands=True`` and the band height is derived from the budget;
otherwise a single-pass conversion is used. Both paths produce bitwise
identical arrays.

Geometry: the scale factor is ``dpi / 72`` and ``px_per_mm = dpi / 25.4``
exactly. Every result carries both, so nothing downstream may assume a DPI.
"""

from __future__ import annotations

import ctypes
import math
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from loguru import logger

from engine.utils.errors import UnreadableFileError, ValidationError
from engine.utils.pdf_runtime import open_document

#: PDF points per millimetre (25.4 mm per inch, 72 points per inch).
POINTS_PER_MM = 72.0 / 25.4

#: Convention shared with `engine.ingest.pdf_inspector`: below this many
#: characters a page has no usable text layer.
MIN_TEXT_CHARS = 20

#: A page counts as scanned when one image covers more than this fraction.
LARGE_IMAGE_COVERAGE = 0.6

#: Pixels whose absolute channel change stays within this are not part of
#: the annotation layer. Renders are deterministic, so unchanged pixels
#: differ by exactly 0; the epsilon only swallows sub-threshold blending.
ANNOT_DIFF_EPSILON = 1

#: Conservative bytes-per-pixel estimate for the pdfium staging decision.
#: pdfium's native bitmap sizing is 4 bytes per pixel (BGRA) even though the
#: packed BGR/RGB buffers actually used here are 3 bytes per pixel.
BYTES_PER_PX_ESTIMATE = 4

#: Pixels below this gray value count as ink for `estimate_ink_coverage`.
INK_DARK_THRESHOLD = 200


@dataclass(slots=True)
class RenderOptions:
    """Tunables for one page render."""

    dpi: int = 200
    colour: bool = True
    #: Render a second pass with annotations and return the difference.
    separate_annotations: bool = False
    grayscale_for_compare: bool = True
    #: Full render estimate above this triggers the banded memory path (MB).
    memory_budget_mb: int = 200


@dataclass(slots=True)
class RenderResult:
    """One rendered page plus everything needed to interpret its pixels.

    ``colour`` and ``grayscale`` are always the *content* render
    (``draw_annots=False``), so alignment and diffing never chase
    annotations. When :attr:`RenderOptions.separate_annotations` is set,
    ``annotations_colour`` holds the with-annotations pixel colours wherever
    the page changed (zero elsewhere) and ``annotations_mask`` marks those
    pixels; compositing ``colour`` with the layer where the mask is set
    reproduces pdfium's with-annotations render exactly.
    """

    page_index: int
    dpi: int
    width_px: int
    height_px: int
    px_per_mm: float
    mm_width: float
    mm_height: float
    pdf_rotation: int
    applied_rotation: int
    colour: np.ndarray | None
    grayscale: np.ndarray | None
    annotations_colour: np.ndarray | None = None
    annotations_mask: np.ndarray | None = None
    rendered_in_bands: bool = False
    duration_s: float = 0.0


def _validate_options(options: RenderOptions) -> None:
    if options.dpi < 1:
        raise ValidationError(
            "The render resolution must be at least 1 DPI.",
            detail={"dpi": options.dpi},
        )
    if options.memory_budget_mb < 1:
        raise ValidationError(
            "The render memory budget must be at least 1 MB.",
            detail={"memory_budget_mb": options.memory_budget_mb},
        )


def _render_canvas(page: pdfium.PdfPage, *, scale: float, draw_annots: bool) -> np.ndarray:
    """Render the full page into a fresh contiguous RGB uint8 canvas.

    Runs inside the pdfium lock (the document is already open). The canvas
    is allocated once here and pdfium draws straight into its memory through
    the ``bitmap_maker`` hook, so no second full-page buffer ever exists.
    """

    holder: dict[str, np.ndarray] = {}

    def bitmap_maker(
        width: int,
        height: int,
        format: int,  # the pdfium format constant, passed through by pypdfium2
        rev_byteorder: bool = False,
    ) -> pdfium.PdfBitmap:
        canvas = np.empty((height, width, 3), dtype=np.uint8)
        buffer = (ctypes.c_ubyte * canvas.nbytes).from_buffer(canvas)
        holder["canvas"] = canvas
        return pdfium.PdfBitmap.new_native(
            width, height, format, rev_byteorder=rev_byteorder, buffer=buffer
        )

    bitmap = page.render(
        scale=scale,
        rotation=0,
        crop=(0.0, 0.0, 0.0, 0.0),
        draw_annots=draw_annots,
        rev_byteorder=True,
        bitmap_maker=bitmap_maker,
    )
    bitmap.close()
    return holder["canvas"]


def _grayscale_from(content: np.ndarray, band_rows: int) -> np.ndarray:
    """Grayscale of a contiguous RGB canvas, converted in row bands."""
    height, width = content.shape[:2]
    gray = np.empty((height, width), dtype=np.uint8)
    for y0 in range(0, height, band_rows):
        y1 = min(y0 + band_rows, height)
        cv2.cvtColor(content[y0:y1], cv2.COLOR_RGB2GRAY, dst=gray[y0:y1])
    return gray


def _annotation_layer(
    content: np.ndarray, annotated: np.ndarray, band_rows: int
) -> tuple[np.ndarray, np.ndarray]:
    """Split the annotation paint off the with-annotations render.

    The two renders share identical content rasterisation, so any pixel
    change is caused by an annotation. Returns (``annotations_colour``,
    ``annotations_mask``): the layer keeps the with-annotations colours where
    the page changed and the mask is 255 on exactly those pixels.
    """
    height, width = content.shape[:2]
    layer = np.zeros_like(content)
    mask = np.zeros((height, width), dtype=np.uint8)
    for y0 in range(0, height, band_rows):
        y1 = min(y0 + band_rows, height)
        delta = np.abs(content[y0:y1].astype(np.int16) - annotated[y0:y1].astype(np.int16))
        changed = np.max(delta, axis=2) > ANNOT_DIFF_EPSILON
        mask[y0:y1][changed] = 255
        layer[y0:y1] = np.where(changed[..., None], annotated[y0:y1], 0)
    return layer, mask


def render_page(
    pdf_path: str | Path,
    page_index: int = 0,
    options: RenderOptions | None = None,
) -> RenderResult:
    """Rasterise one PDF page to numpy arrays at the requested DPI.

    The document is opened through :func:`engine.utils.pdf_runtime.open_document`,
    which holds the process-wide pdfium lock for the whole render. All
    pdfium failures surface as :class:`UnreadableFileError` with user-facing
    wording; a page index outside the document is a
    :class:`ValidationError`.
    """
    opts = options if options is not None else RenderOptions()
    _validate_options(opts)
    scale = opts.dpi / 72.0
    started = time.perf_counter()
    target = str(pdf_path)

    want_gray = opts.grayscale_for_compare
    want_colour = opts.colour or opts.separate_annotations or want_gray

    try:
        with open_document(target) as document:
            if page_index < 0 or page_index >= len(document):
                raise ValidationError(
                    f"That file has only {len(document)} page"
                    f"{'s' if len(document) != 1 else ''}, so page "
                    f"{page_index + 1} could not be rendered.",
                    detail={"page_index": page_index, "page_count": len(document)},
                )
            page = document[page_index]
            pdf_rotation = page.get_rotation()
            width_pt, height_pt = page.get_size()
            width_px = math.ceil(width_pt * scale)
            height_px = math.ceil(height_pt * scale)

            budget_bytes = opts.memory_budget_mb * 1024 * 1024
            estimate_bytes = width_px * height_px * BYTES_PER_PX_ESTIMATE
            rendered_in_bands = estimate_bytes > budget_bytes
            if rendered_in_bands:
                band_rows = max(1, budget_bytes // max(1, width_px * 4))
            else:
                band_rows = height_px

            colour: np.ndarray | None = None
            grayscale: np.ndarray | None = None
            annotations_colour: np.ndarray | None = None
            annotations_mask: np.ndarray | None = None

            if want_colour:
                content = _render_canvas(page, scale=scale, draw_annots=False)
                if want_gray:
                    grayscale = _grayscale_from(content, band_rows)
                if opts.separate_annotations:
                    annotated = _render_canvas(page, scale=scale, draw_annots=True)
                    annotations_colour, annotations_mask = _annotation_layer(
                        content, annotated, band_rows
                    )
                if opts.colour:
                    colour = content
    except pdfium.PdfiumError as exc:
        logger.debug("pdfium could not render page {} of {}: {}", page_index, target, exc)
        raise UnreadableFileError(
            detail={"path": target, "page_index": page_index, "cause": str(exc)}
        ) from None
    except OSError as exc:
        logger.debug("could not open {} for rendering: {}", target, exc)
        raise UnreadableFileError(
            detail={"path": target, "page_index": page_index, "cause": str(exc)}
        ) from None

    duration_s = time.perf_counter() - started
    logger.debug(
        "Rendered page {} of {} | {}x{} px @ {} dpi | banded={} | {:.3f}s",
        page_index,
        target,
        width_px,
        height_px,
        opts.dpi,
        rendered_in_bands,
        duration_s,
    )
    return RenderResult(
        page_index=page_index,
        dpi=opts.dpi,
        width_px=width_px,
        height_px=height_px,
        px_per_mm=opts.dpi / 25.4,
        mm_width=width_pt / POINTS_PER_MM,
        mm_height=height_pt / POINTS_PER_MM,
        pdf_rotation=pdf_rotation,
        applied_rotation=0,
        colour=colour,
        grayscale=grayscale,
        annotations_colour=annotations_colour,
        annotations_mask=annotations_mask,
        rendered_in_bands=rendered_in_bands,
        duration_s=duration_s,
    )


def detect_scanned(page: pdfium.PdfPage) -> bool:
    """True when the page looks like a photocopy, not a vector drawing.

    A single image object covering most of the page and (almost) no text.
    Call only while the owning document is open inside
    :func:`engine.utils.pdf_runtime.open_document`.
    """
    text_chars = 0
    textpage: pdfium.PdfTextPage | None = None
    try:
        textpage = page.get_textpage()
        text_chars = len(textpage.get_text_range().strip())
    except pdfium.PdfiumError:
        text_chars = 0
    finally:
        if textpage is not None:
            textpage.close()

    if text_chars >= MIN_TEXT_CHARS:
        return False

    try:
        images = list(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]))
    except pdfium.PdfiumError:
        return False
    if not images:
        return False

    width_pt, height_pt = page.get_size()
    page_area = width_pt * height_pt
    if page_area <= 0:
        return False

    largest = 0.0
    for image in images:
        try:
            left, bottom, right, top = image.get_bounds()
        except pdfium.PdfiumError:
            # Without bounds this object cannot prove coverage; skip it.
            continue
        largest = max(largest, abs(right - left) * abs(top - bottom))
    return largest / page_area > LARGE_IMAGE_COVERAGE


def _as_grayscale(image: np.ndarray) -> np.ndarray:
    """Validate a uint8 image and reduce RGB input to grayscale."""
    array = np.asarray(image)
    if array.dtype != np.uint8:
        raise ValueError(f"Expected a uint8 image, got {array.dtype}.")
    if array.ndim == 3:
        if array.shape[2] != 3:
            raise ValueError(f"Expected an RGB image, got shape {array.shape}.")
        return cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    if array.ndim != 2:
        raise ValueError(f"Expected a 2-D grayscale image, got shape {array.shape}.")
    return array


def estimate_ink_coverage(image: np.ndarray) -> float:
    """Fraction of pixels darker than 200 in the grayscale of *image*."""
    gray = _as_grayscale(image)
    return float(np.mean(gray < INK_DARK_THRESHOLD))


def binarize(image: np.ndarray, method: str = "otsu") -> np.ndarray:
    """Binarize a grayscale or RGB image to ``{0, 255}``.

    ``method="otsu"`` uses a global Otsu threshold; ``method="adaptive"``
    uses Gaussian adaptive thresholding with block size 35 and offset 15.
    """
    gray = _as_grayscale(image)
    if method == "otsu":
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return binary
    if method == "adaptive":
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 15
        )
    raise ValueError(f"Unknown binarize method {method!r}; use 'otsu' or 'adaptive'.")
