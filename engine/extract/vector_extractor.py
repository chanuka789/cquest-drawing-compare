"""Task 5.8 (extraction) — reading real geometry out of a PDF.

When a drawing was plotted from CAD, the PDF carries the actual lines, not a
picture of them. Comparing that geometry instead of pixels removes every
source of raster noise at once: line weight, anti-aliasing, resampling, the
plotter's colour table. It is the difference between a report with twelve
changes and a report with four hundred.

Three facts shape this module:

* **pdfium is not thread-safe.** Every document here goes through
  :func:`engine.utils.pdf_runtime.open_document`, which holds the global
  lock, exactly as the render and text paths do.
* **A page object's matrix is its own, not the composed one.** An object
  inside a form XObject is positioned by its own matrix *and* every matrix
  above it. Composing the chain is not optional — miss it and a title block
  drawn as a form lands on top of the drawing.
* **Optional content lives in the catalogue, not in the objects.** pdfium can
  say an object is inside *a* marked-content group but not which layer that
  is, so the layer names and their on/off state are read from
  ``/OCProperties`` with pikepdf. Comparing the two catalogues is also the
  most reliable way to detect a toggled layer, which is worth one change
  record and never thousands.

Coordinates come out in **image pixels at the requested DPI, y down** — the
same space the raster and text streams use, so the three can be compared
without another conversion.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from loguru import logger

from engine.align.anchors import page_points_to_image_px
from engine.compare.types import Bbox
from engine.utils.pdf_runtime import open_document, pdfium_access

#: A PDF path made of fewer objects than this is almost certainly a scanned
#: page with a frame drawn round it. The vector stream skips those and says
#: so, rather than reporting a confident result from three rectangles.
MIN_PATHS_FOR_VECTOR_STREAM = 50

#: pdfium segment types.
SEGMENT_UNKNOWN = -1
SEGMENT_LINETO = 0
SEGMENT_BEZIERTO = 1
SEGMENT_MOVETO = 2


@dataclass(slots=True)
class Segment:
    """One path segment, in image pixels."""

    kind: int
    points: list[tuple[float, float]] = field(default_factory=list)
    closes: bool = False


@dataclass(slots=True)
class RawPath:
    """One path object off the page, with its graphics state.

    Geometry and style are kept apart on purpose: two lines with the same
    points and different pens are the same line, replotted. That distinction
    is what lets the comparison report a line weight change as cosmetic
    instead of as every line on the sheet changing.
    """

    segments: list[Segment] = field(default_factory=list)
    stroked: bool = False
    filled: bool = False
    #: Line width in image pixels.
    line_width_px: float = 0.0
    stroke_colour: tuple[int, int, int, int] = (0, 0, 0, 255)
    fill_colour: tuple[int, int, int, int] = (0, 0, 0, 0)
    dash_array: tuple[float, ...] = ()
    dash_phase: float = 0.0
    #: Marked-content tag names on the object, e.g. ``("OC",)``.
    marks: tuple[str, ...] = ()
    #: Nesting level; anything above 0 came out of a form XObject.
    level: int = 0
    bbox: Bbox = field(default_factory=lambda: Bbox(0.0, 0.0, 0.0, 0.0))

    @property
    def point_count(self) -> int:
        return sum(len(segment.points) for segment in self.segments)

    @property
    def is_degenerate(self) -> bool:
        """Zero-length paths and single points draw nothing."""
        return self.point_count < 2 or (self.bbox.w <= 0.01 and self.bbox.h <= 0.01)

    @property
    def in_optional_content(self) -> bool:
        return "OC" in self.marks


@dataclass(slots=True)
class LayerInfo:
    """One optional content group, and whether it is switched on."""

    name: str
    visible: bool = True
    #: pdfium/pikepdf object id, for matching the same layer across sheets.
    object_key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "visible": self.visible}


@dataclass(slots=True)
class VectorPage:
    """Everything the vector stream needs about one page."""

    paths: list[RawPath] = field(default_factory=list)
    layers: list[LayerInfo] = field(default_factory=list)
    width_px: int = 0
    height_px: int = 0
    dpi: int = 200
    #: Set when the stream should not run, with the reason for the record.
    skip_reason: str = ""

    @property
    def usable(self) -> bool:
        return not self.skip_reason and len(self.paths) >= MIN_PATHS_FOR_VECTOR_STREAM


# ── Matrix helpers ──────────────────────────────────────────────────────

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def compose(inner: Matrix, outer: Matrix) -> Matrix:
    """``inner`` applied first, then ``outer`` — PDF composition order."""
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _matrix_scale(matrix: Matrix) -> float:
    """The uniform scale a matrix applies, for line widths."""
    a, b, c, d, _e, _f = matrix
    return float(abs(a * d - b * c) ** 0.5) or 1.0


# ── Extraction ──────────────────────────────────────────────────────────


def extract_page_paths(
    page: pdfium.PdfPage, *, dpi: int = 200, max_paths: int = 200_000
) -> list[RawPath]:
    """Every path object on *page*, in image pixels at *dpi*.

    Never raises for a page that cannot be walked: an empty list means "no
    usable vectors", which is a real answer (the sheet is scanned) and is
    recorded as the reason the stream was skipped.
    """
    with pdfium_access():
        return _read_paths(page, dpi=dpi, max_paths=max_paths)


def _page_box(page: pdfium.PdfPage) -> tuple[float, float, float, float]:
    try:
        box = page.get_mediabox()
        if box and len(box) == 4:
            x0, y0, x1, y1 = (float(value) for value in box)
            if x1 > x0 and y1 > y0:
                return x0, y0, x1 - x0, y1 - y0
    except (pdfium.PdfiumError, TypeError, ValueError):
        pass
    width, height = page.get_size()
    return 0.0, 0.0, float(width), float(height)


def _read_paths(page: pdfium.PdfPage, *, dpi: int, max_paths: int) -> list[RawPath]:
    box_x0, box_y0, width_pt, height_pt = _page_box(page)

    def to_px(x: float, y: float) -> tuple[float, float]:
        return page_points_to_image_px(x, y, box_x0, box_y0, width_pt, height_pt, dpi)

    paths: list[RawPath] = []
    try:
        objects = list(page.get_objects(max_depth=8))
    except pdfium.PdfiumError as exc:
        logger.debug("Could not walk page objects: {}", exc)
        return []

    for obj in objects:
        if obj.type != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        if len(paths) >= max_paths:
            logger.warning("Stopped reading paths at {} objects", max_paths)
            break
        raw = _read_one_path(obj, to_px, dpi)
        if raw is not None:
            paths.append(raw)

    return paths


def _object_matrix(obj: pdfium.PdfObject) -> Matrix:
    """The object's matrix composed with every form matrix above it."""
    try:
        own = obj.get_matrix()
        matrix: Matrix = (own.a, own.b, own.c, own.d, own.e, own.f)
    except pdfium.PdfiumError:
        matrix = IDENTITY

    parent = getattr(obj, "parent", None)
    while isinstance(parent, pdfium.PdfObject):
        try:
            outer = parent.get_matrix()
            matrix = compose(matrix, (outer.a, outer.b, outer.c, outer.d, outer.e, outer.f))
        except pdfium.PdfiumError:
            pass
        parent = getattr(parent, "parent", None)
    return matrix


def _read_one_path(
    obj: pdfium.PdfObject,
    to_px: Any,
    dpi: int,
) -> RawPath | None:
    count = pdfium_c.FPDFPath_CountSegments(obj)
    if count <= 0:
        return None

    matrix = _object_matrix(obj)
    segments: list[Segment] = []
    points: list[tuple[float, float]] = []
    pending: list[tuple[float, float]] = []

    for index in range(count):
        segment = pdfium_c.FPDFPath_GetPathSegment(obj, index)
        if not segment:
            continue
        x = ctypes.c_float()
        y = ctypes.c_float()
        if not pdfium_c.FPDFPathSegment_GetPoint(segment, x, y):
            continue
        kind = pdfium_c.FPDFPathSegment_GetType(segment)
        closes = bool(pdfium_c.FPDFPathSegment_GetClose(segment))
        page_x, page_y = _apply(matrix, float(x.value), float(y.value))
        point = to_px(page_x, page_y)
        points.append(point)

        if kind == SEGMENT_BEZIERTO:
            # pdfium reports a cubic as three consecutive BEZIERTO points.
            pending.append(point)
            if len(pending) == 3:
                segments.append(Segment(SEGMENT_BEZIERTO, list(pending), closes))
                pending = []
        else:
            if pending:
                segments.append(Segment(SEGMENT_BEZIERTO, list(pending), False))
                pending = []
            segments.append(Segment(kind, [point], closes))

    if pending:
        segments.append(Segment(SEGMENT_BEZIERTO, list(pending), False))
    if not segments:
        return None

    fill_mode = ctypes.c_int()
    stroke = ctypes.c_int()
    pdfium_c.FPDFPath_GetDrawMode(obj, fill_mode, stroke)

    scale = _matrix_scale(matrix) * dpi / 72.0
    width = ctypes.c_float()
    has_width = bool(pdfium_c.FPDFPageObj_GetStrokeWidth(obj, width))
    line_width = float(width.value) * scale if has_width else 0.0

    return RawPath(
        segments=segments,
        stroked=bool(stroke.value),
        filled=fill_mode.value != 0,
        line_width_px=line_width,
        stroke_colour=_colour(obj, stroke=True),
        fill_colour=_colour(obj, stroke=False),
        dash_array=_dash_array(obj, scale),
        dash_phase=_dash_phase(obj) * scale,
        marks=_marks(obj),
        level=int(getattr(obj, "level", 0) or 0),
        bbox=Bbox.from_points(points),
    )


def _colour(obj: pdfium.PdfObject, *, stroke: bool) -> tuple[int, int, int, int]:
    red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
    getter = pdfium_c.FPDFPageObj_GetStrokeColor if stroke else pdfium_c.FPDFPageObj_GetFillColor
    if not getter(obj, red, green, blue, alpha):
        return (0, 0, 0, 255 if stroke else 0)
    return (int(red.value), int(green.value), int(blue.value), int(alpha.value))


def _dash_array(obj: pdfium.PdfObject, scale: float) -> tuple[float, ...]:
    count = pdfium_c.FPDFPageObj_GetDashCount(obj)
    if count <= 0:
        return ()
    buffer = (ctypes.c_float * count)()
    if not pdfium_c.FPDFPageObj_GetDashArray(obj, buffer, count):
        return ()
    return tuple(round(float(value) * scale, 3) for value in buffer)


def _dash_phase(obj: pdfium.PdfObject) -> float:
    phase = ctypes.c_float()
    if not pdfium_c.FPDFPageObj_GetDashPhase(obj, phase):
        return 0.0
    return float(phase.value)


def _marks(obj: pdfium.PdfObject) -> tuple[str, ...]:
    count = pdfium_c.FPDFPageObj_CountMarks(obj)
    if count <= 0:
        return ()
    names: list[str] = []
    for index in range(count):
        mark = pdfium_c.FPDFPageObj_GetMark(obj, index)
        if not mark:
            continue
        # The name comes back as UTF-16LE in an FPDF_WCHAR buffer, so the
        # buffer has to be c_ushort, not the usual char array.
        length = ctypes.c_ulong()
        buffer = (ctypes.c_ushort * 128)()
        if pdfium_c.FPDFPageObjMark_GetName(
            mark, buffer, ctypes.sizeof(buffer), ctypes.byref(length)
        ):
            raw = bytes(buffer)[: length.value]
            name = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
            if name:
                names.append(name)
    return tuple(names)


# ── Optional content ────────────────────────────────────────────────────


def read_layers(pdf_path: str | Path) -> list[LayerInfo]:
    """Optional content groups and their default visibility.

    pdfium exposes that an object carries an ``/OC`` mark but not which group
    it belongs to, so the catalogue is read directly. Comparing two sheets'
    catalogues is also the cleanest way to spot a toggled layer: one record
    naming the layer, rather than every object on it reported as removed.
    """
    try:
        with pikepdf.Pdf.open(str(pdf_path)) as pdf:
            properties = pdf.Root.get("/OCProperties")
            if properties is None:
                return []
            default = properties.get("/D", pikepdf.Dictionary())
            off_keys = {_object_key(item) for item in default.get("/OFF", [])}
            base_off = str(default.get("/BaseState", "/ON")) == "/OFF"
            on_keys = {_object_key(item) for item in default.get("/ON", [])}

            layers: list[LayerInfo] = []
            for group in properties.get("/OCGs", []):
                key = _object_key(group)
                name = str(group.get("/Name", "")) or f"Layer {len(layers) + 1}"
                visible = (key in on_keys) if base_off else (key not in off_keys)
                layers.append(LayerInfo(name=name, visible=visible, object_key=key))
            return layers
    except (pikepdf.PdfError, OSError) as exc:
        logger.debug("Could not read optional content from {}: {}", pdf_path, exc)
        return []


def _object_key(obj: Any) -> str:
    objgen = getattr(obj, "objgen", None)
    if objgen:
        return f"{objgen[0]}:{objgen[1]}"
    return str(obj)


# ── Page-level entry point ──────────────────────────────────────────────


def extract_vector_page(
    pdf_path: str | Path,
    page_index: int = 0,
    *,
    dpi: int = 200,
    width_px: int = 0,
    height_px: int = 0,
) -> VectorPage:
    """Read one page's geometry, or record why the vector stream cannot run."""
    result = VectorPage(dpi=dpi, width_px=width_px, height_px=height_px)
    try:
        with open_document(str(pdf_path)) as document:
            if not 0 <= page_index < len(document):
                result.skip_reason = "That page is not in the file."
                return result
            page = document[page_index]
            result.paths = extract_page_paths(page, dpi=dpi)
            if not result.width_px:
                _, _, width_pt, height_pt = _page_box(page)
                result.width_px = round(width_pt * dpi / 72.0)
                result.height_px = round(height_pt * dpi / 72.0)
    except pdfium.PdfiumError as exc:
        result.skip_reason = f"The drawing's geometry could not be read ({exc})."
        return result

    result.layers = read_layers(pdf_path)

    if len(result.paths) < MIN_PATHS_FOR_VECTOR_STREAM:
        result.skip_reason = (
            f"This sheet carries only {len(result.paths)} drawn objects, so it is a "
            "scan rather than a plot. The comparison used the image instead."
        )
    logger.debug(
        "Vector extraction | paths={} | layers={} | skip={}",
        len(result.paths),
        len(result.layers),
        result.skip_reason or "no",
    )
    return result
