"""Task 5.0 — the change-injection harness. Phase 5's ground truth.

Phase 4 could measure itself because a synthetic pair knows its own transform.
Phase 5 needs the same thing for content: a pair of sheets where the exact
list of real changes is known, so precision and recall are measured rather
than felt.

Two kinds of injection, and the difference between them is the whole point:

* **Genuine** changes — a moved object, a deleted object, a changed dimension,
  a different hatch pattern. Each one records an expected bounding box. Recall
  is how many of these the engine found.
* **Cosmetic** changes — a heavier pen, a dashed line type, a substituted
  font, a monochrome plot, a watermark, a bumped revision letter, a toggled
  layer. These record *nothing*. The headline metric of the whole phase is
  that a pair carrying only cosmetic injections reports **zero** changes.

Everything works on the content stream of a fixture page, which is plain
ASCII PDF operators produced by :mod:`tests.fixture_builder`. That is enough
to move, delete, add, restyle and relabel real drawn objects without ever
touching a user's file — and it keeps the ground truth exact, because the
harness knows precisely which operators it changed.

Coordinates in and out of this module are PDF user-space points on the page
(y up, page-box origin), the same space :mod:`engine.extract.text_extractor`
reports. :mod:`tests.harness.precision_recall` converts them to comparison
pixels once.
"""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf

# ── Content stream tokens ───────────────────────────────────────────────

#: One PDF operand: a number, a name, a literal string or an array.
_TOKEN = re.compile(
    r"""
    (?P<string>\((?:\\.|[^()\\])*\))   # (literal string) with escapes
  | (?P<array>\[[^\]]*\])              # [1 2] dash arrays
  | (?P<name>/[^\s/\[\]()<>]+)         # /F1
  | (?P<number>[-+]?\d*\.?\d+)         # 12  -3.5  .25
  | (?P<operator>[A-Za-z'"*]+)         # re  S  Tj  T*
    """,
    re.VERBOSE,
)

#: Operators that paint and therefore end a drawable element.
_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}

#: Operators carrying (x, y) pairs that a move rewrites.
_POINT_OPS = {"m": 1, "l": 1, "c": 3, "v": 2, "y": 2}


@dataclass(slots=True)
class Op:
    """One operator with its operands, and the exact text it came from."""

    operands: list[str]
    operator: str

    def render(self) -> str:
        if not self.operands:
            return self.operator
        return " ".join(self.operands) + " " + self.operator

    def numbers(self) -> list[float]:
        values: list[float] = []
        for operand in self.operands:
            try:
                values.append(float(operand))
            except ValueError:
                values.append(math.nan)
        return values


def tokenise(stream: str) -> list[Op]:
    """Split a content stream into operations. Comments are not expected."""
    ops: list[Op] = []
    operands: list[str] = []
    for match in _TOKEN.finditer(stream):
        kind = match.lastgroup
        text = match.group()
        if kind == "operator":
            ops.append(Op(operands, text))
            operands = []
        else:
            operands.append(text)
    return ops


def render(ops: list[Op]) -> str:
    return "\n".join(op.render() for op in ops)


# ── Elements: the drawable things an injection can act on ────────────────

#: A 2x3 affine, PDF cm order (a, b, c, d, e, f).
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(first: Matrix, second: Matrix) -> Matrix:
    """``first`` applied inside ``second`` — PDF's ``cm`` composition order."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply_matrix(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def inverse_linear(matrix: Matrix, dx: float, dy: float) -> tuple[float, float]:
    """A page-space delta expressed in the stream space under *matrix*."""
    a, b, c, d, _e, _f = matrix
    determinant = a * d - b * c
    if abs(determinant) < 1e-12:
        return dx, dy
    return (d * dx - c * dy) / determinant, (-b * dx + a * dy) / determinant


#: Helvetica is close enough to half an em per character for a fixture bbox.
_GLYPH_WIDTH_EM = 0.55


@dataclass(slots=True)
class Element:
    """One drawn thing: a path, or a BT/ET text block."""

    kind: str  # "path" | "text" | "state"
    ops: list[Op]
    ctm: Matrix = IDENTITY
    #: Page-space bounding box (x0, y0, x1, y1), y up. None for state runs.
    bbox: tuple[float, float, float, float] | None = None
    text: str = ""
    font_size: float = 0.0
    #: What injected this element, so a later injection can replace it.
    tag: str = ""

    @property
    def is_drawable(self) -> bool:
        return self.kind in {"path", "text"}


def _string_value(token: str) -> str:
    inner = token[1:-1]
    return re.sub(r"\\(.)", r"\1", inner)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _path_bbox(ops: list[Op], ctm: Matrix) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    for op in ops:
        if op.operator == "re" and len(op.operands) >= 4:
            x, y, w, h = (float(value) for value in op.operands[:4])
            for corner in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                points.append(apply_matrix(ctm, *corner))
        elif op.operator in _POINT_OPS:
            values = op.numbers()
            for index in range(0, len(values) - 1, 2):
                points.append(apply_matrix(ctm, values[index], values[index + 1]))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _text_of(ops: list[Op]) -> tuple[str, float, tuple[float, float] | None, Matrix | None]:
    """The string, its size, its Td origin and its Tm, from one BT/ET run."""
    text = ""
    size = 0.0
    origin: tuple[float, float] | None = None
    text_matrix: Matrix | None = None
    for op in ops:
        if op.operator == "Tf" and len(op.operands) >= 2:
            size = float(op.operands[1])
        elif op.operator == "Td" and len(op.operands) >= 2:
            origin = (float(op.operands[0]), float(op.operands[1]))
        elif op.operator == "Tm" and len(op.operands) >= 6:
            values = [float(value) for value in op.operands[:6]]
            text_matrix = (values[0], values[1], values[2], values[3], values[4], values[5])
            origin = (values[4], values[5])
        elif op.operator == "Tj" and op.operands:
            text += _string_value(op.operands[-1])
    return text, size, origin, text_matrix


def parse_elements(ops: list[Op]) -> list[Element]:
    """Group a token stream into drawable elements, tracking the CTM."""
    elements: list[Element] = []
    stack: list[Matrix] = []
    ctm: Matrix = IDENTITY
    pending: list[Op] = []
    in_text = False

    def flush_state() -> None:
        nonlocal pending
        if pending:
            elements.append(Element(kind="state", ops=pending, ctm=ctm))
            pending = []

    for op in ops:
        if op.operator == "q":
            pending.append(op)
            stack.append(ctm)
            continue
        if op.operator == "Q":
            pending.append(op)
            ctm = stack.pop() if stack else IDENTITY
            continue
        if op.operator == "cm" and len(op.operands) >= 6:
            pending.append(op)
            values = [float(value) for value in op.operands[:6]]
            ctm = multiply((values[0], values[1], values[2], values[3], values[4], values[5]), ctm)
            continue
        if op.operator == "BT":
            flush_state()
            in_text = True
            pending = [op]
            continue
        if op.operator == "ET":
            pending.append(op)
            text, size, origin, text_matrix = _text_of(pending)
            bbox = None
            if origin is not None:
                local = text_matrix if text_matrix else IDENTITY
                width = _GLYPH_WIDTH_EM * size * max(len(text), 1)
                corners = [(0.0, 0.0), (width, 0.0), (0.0, size), (width, size)]
                mapped = []
                for x, y in corners:
                    if text_matrix:
                        # Tm already carries the origin; use its linear part.
                        tx = local[0] * x + local[2] * y + local[4]
                        ty = local[1] * x + local[3] * y + local[5]
                    else:
                        tx, ty = origin[0] + x, origin[1] + y
                    mapped.append(apply_matrix(ctm, tx, ty))
                xs = [point[0] for point in mapped]
                ys = [point[1] for point in mapped]
                bbox = (min(xs), min(ys), max(xs), max(ys))
            elements.append(
                Element(kind="text", ops=pending, ctm=ctm, bbox=bbox, text=text, font_size=size)
            )
            pending = []
            in_text = False
            continue
        if in_text:
            pending.append(op)
            continue

        pending.append(op)
        if op.operator in _PAINT:
            bbox = _path_bbox(pending, ctm)
            elements.append(Element(kind="path", ops=pending, ctm=ctm, bbox=bbox))
            pending = []

    flush_state()
    return elements


def flatten(elements: list[Element]) -> list[Op]:
    ops: list[Op] = []
    for element in elements:
        ops.extend(element.ops)
    return ops


# ── Injections ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExpectedChange:
    """One change the engine is expected to report, with its true box."""

    kind: str
    #: (x0, y0, x1, y1) in new-page user-space points, y up.
    bbox_pt: tuple[float, float, float, float]
    label: str = ""
    old_text: str | None = None
    new_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bbox_pt": list(self.bbox_pt),
            "label": self.label,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass(slots=True)
class InjectionContext:
    """What an injection is allowed to touch."""

    elements: list[Element]
    pdf: pikepdf.Pdf
    page: pikepdf.Object
    width: float
    height: float
    #: Page box origin, so a fixture with a centred media box still works.
    x0: float
    y0: float


class Injection:
    """Base class. ``apply`` mutates the context and returns expectations."""

    #: Cosmetic injections must produce no reported change at all.
    cosmetic: bool = False
    name: str = "injection"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


def _inside(
    bbox: tuple[float, float, float, float], region: tuple[float, float, float, float]
) -> bool:
    x0, y0, x1, y1 = bbox
    rx0, ry0, rx1, ry1 = region
    return x0 >= rx0 and y0 >= ry0 and x1 <= rx1 and y1 <= ry1


@dataclass(slots=True)
class MoveObject(Injection):
    """Shift every object fully inside *region* by a paper-millimetre delta."""

    region: tuple[float, float, float, float]
    dx_mm: float
    dy_mm: float
    cosmetic = False
    name = "move_object"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        dx = self.dx_mm * 72.0 / 25.4
        dy = self.dy_mm * 72.0 / 25.4
        touched: list[tuple[float, float, float, float]] = []
        for element in context.elements:
            if not element.is_drawable or element.bbox is None:
                continue
            if not _inside(element.bbox, self.region):
                continue
            local_dx, local_dy = inverse_linear(element.ctm, dx, dy)
            _shift_element(element, local_dx, local_dy)
            touched.append(element.bbox)
            element.bbox = (
                element.bbox[0] + dx,
                element.bbox[1] + dy,
                element.bbox[2] + dx,
                element.bbox[3] + dy,
            )
        if not touched:
            return []
        before = _union(touched)
        after = (before[0] + dx, before[1] + dy, before[2] + dx, before[3] + dy)
        return [ExpectedChange("moved", _union([before, after]), label="moved object")]


def _shift_element(element: Element, dx: float, dy: float) -> None:
    for op in element.ops:
        if op.operator == "re" and len(op.operands) >= 4:
            x, y = float(op.operands[0]), float(op.operands[1])
            op.operands[0] = _num(x + dx)
            op.operands[1] = _num(y + dy)
        elif op.operator in _POINT_OPS:
            values = op.numbers()
            for index in range(0, len(values) - 1, 2):
                op.operands[index] = _num(values[index] + dx)
                op.operands[index + 1] = _num(values[index + 1] + dy)
        elif op.operator == "Td" and len(op.operands) >= 2:
            op.operands[0] = _num(float(op.operands[0]) + dx)
            op.operands[1] = _num(float(op.operands[1]) + dy)
        elif op.operator == "Tm" and len(op.operands) >= 6:
            op.operands[4] = _num(float(op.operands[4]) + dx)
            op.operands[5] = _num(float(op.operands[5]) + dy)


def _num(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


@dataclass(slots=True)
class DeleteObject(Injection):
    """Erase every object fully inside *region*."""

    region: tuple[float, float, float, float]
    cosmetic = False
    name = "delete_object"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        removed: list[tuple[float, float, float, float]] = []
        keep: list[Element] = []
        for element in context.elements:
            if (
                element.is_drawable
                and element.bbox is not None
                and _inside(element.bbox, self.region)
            ):
                removed.append(element.bbox)
                continue
            keep.append(element)
        context.elements[:] = keep
        if not removed:
            return []
        return [ExpectedChange("removed", _union(removed), label="deleted object")]


@dataclass(slots=True)
class AddObject(Injection):
    """Draw a new rectangle, circle or line inside *region*."""

    region: tuple[float, float, float, float]
    shape: str = "rect"
    line_width: float = 1.2
    cosmetic = False
    name = "add_object"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        x0, y0, x1, y1 = self.region
        ops: list[Op]
        if self.shape == "circle":
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            radius = min(x1 - x0, y1 - y0) / 2
            ops = tokenise(_circle_ops(cx, cy, radius, self.line_width))
        elif self.shape == "line":
            ops = tokenise(f"{self.line_width:.2f} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")
        else:
            ops = tokenise(
                f"{self.line_width:.2f} w {x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f} re S"
            )
        context.elements.append(Element(kind="path", ops=ops, ctm=IDENTITY, bbox=self.region))
        return [ExpectedChange("added", self.region, label=f"added {self.shape}")]


def _circle_ops(cx: float, cy: float, radius: float, width: float) -> str:
    k = 0.5522847498
    parts = [f"{width:.2f} w {cx + radius:.2f} {cy:.2f} m"]
    for x1, y1, x2, y2, x3, y3 in (
        (cx + radius, cy + k * radius, cx + k * radius, cy + radius, cx, cy + radius),
        (cx - k * radius, cy + radius, cx - radius, cy + k * radius, cx - radius, cy),
        (cx - radius, cy - k * radius, cx - k * radius, cy - radius, cx, cy - radius),
        (cx + k * radius, cy - radius, cx + radius, cy - k * radius, cx + radius, cy),
    ):
        parts.append(f"{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f} {x3:.2f} {y3:.2f} c")
    parts.append("S")
    return "\n".join(parts)


@dataclass(slots=True)
class ChangeText(Injection):
    """Find a string on the sheet and replace it."""

    old_string: str
    new_string: str
    kind: str = "modified"
    cosmetic = False
    name = "change_text"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        expected: list[ExpectedChange] = []
        for element in context.elements:
            if element.kind != "text" or element.text != self.old_string:
                continue
            for op in element.ops:
                if op.operator == "Tj" and op.operands:
                    op.operands[-1] = f"({_escape(self.new_string)})"
            element.text = self.new_string
            if element.bbox is not None:
                expected.append(
                    ExpectedChange(
                        self.kind,
                        element.bbox,
                        label="text changed",
                        old_text=self.old_string,
                        new_text=self.new_string,
                    )
                )
        return expected


@dataclass(slots=True)
class ChangeDimension(ChangeText):
    """A numeric text change. Same mechanism, different reported meaning."""

    name = "change_dimension"


@dataclass(slots=True)
class RenumberTags(Injection):
    """Bulk tag renumbering: many tags, one consistent mapping."""

    mapping: dict[str, str] = field(default_factory=dict)
    cosmetic = False
    name = "renumber_tags"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        expected: list[ExpectedChange] = []
        for element in context.elements:
            if element.kind != "text":
                continue
            replacement = self.mapping.get(element.text)
            if replacement is None:
                continue
            for op in element.ops:
                if op.operator == "Tj" and op.operands:
                    op.operands[-1] = f"({_escape(replacement)})"
            if element.bbox is not None:
                expected.append(
                    ExpectedChange(
                        "renumbered",
                        element.bbox,
                        label="tag renumbered",
                        old_text=element.text,
                        new_text=replacement,
                    )
                )
            element.text = replacement
        return expected


@dataclass(slots=True)
class ChangeHatch(Injection):
    """Replace the hatch inside *region* with a different angle and spacing."""

    region: tuple[float, float, float, float]
    new_angle_deg: float = 90.0
    new_spacing_pt: float = 6.0
    #: Segments shorter than this are treated as hatch and swept away.
    max_segment_pt: float = 40.0
    cosmetic = False
    name = "change_hatch"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        keep: list[Element] = []
        removed = 0
        for element in context.elements:
            if element.kind != "path" or element.bbox is None:
                keep.append(element)
                continue
            if not _inside(element.bbox, self.region):
                keep.append(element)
                continue
            # Short segments are the sheet's own hatch; a tagged element is
            # hatch an earlier injection drew. Both are replaced, so building
            # a hatched wall and then changing its pattern really is a
            # change of pattern rather than two patterns on top of each other.
            if element.tag == "hatch" or _is_short_segment(element, self.max_segment_pt):
                removed += 1
                continue
            keep.append(element)
        context.elements[:] = keep
        ops = tokenise(_hatch_ops(self.region, self.new_angle_deg, self.new_spacing_pt))
        context.elements.append(
            Element(kind="path", ops=ops, ctm=IDENTITY, bbox=self.region, tag="hatch")
        )
        return [
            ExpectedChange(
                "hatch_pattern_changed",
                self.region,
                label=f"hatch replaced ({removed} segments)",
            )
        ]


def _is_short_segment(element: Element, limit_pt: float) -> bool:
    if element.bbox is None:
        return False
    x0, y0, x1, y1 = element.bbox
    return math.hypot(x1 - x0, y1 - y0) <= limit_pt


def _hatch_ops(region: tuple[float, float, float, float], angle_deg: float, spacing: float) -> str:
    """Parallel lines at *angle_deg* filling *region*, cut to its box.

    The segments are clipped **analytically**, not with a PDF clipping path.
    A clip changes what is painted, not what the path says: a reader
    extracting the geometry gets the full, uncut line. Real CAD hatch is
    drawn as genuinely short segments, and a fixture whose hatch is secretly
    made of sheet-long lines would test the wrong thing entirely.
    """
    x0, y0, x1, y1 = region
    angle = math.radians(angle_deg)
    dx, dy = math.cos(angle), math.sin(angle)
    nx, ny = -dy, dx
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    offsets = [corner[0] * nx + corner[1] * ny for corner in corners]
    length = math.hypot(x1 - x0, y1 - y0) * 2.0

    lines = ["0.18 w"]
    start = math.floor(min(offsets) / spacing) * spacing
    steps = int((max(offsets) - min(offsets)) / spacing) + 2
    for index in range(steps):
        offset = start + index * spacing
        px, py = nx * offset, ny * offset
        segment = _clip_to_box(
            (px - dx * length, py - dy * length),
            (px + dx * length, py + dy * length),
            region,
        )
        if segment is None:
            continue
        (ax, ay), (bx, by) = segment
        if math.hypot(bx - ax, by - ay) < 0.5:
            continue
        lines.append(f"{ax:.2f} {ay:.2f} m {bx:.2f} {by:.2f} l S")
    return "\n".join(lines)


def _clip_to_box(
    start: tuple[float, float],
    end: tuple[float, float],
    box: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang-Barsky: the part of a segment inside an axis-aligned box."""
    x0, y0, x1, y1 = box
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    lower, upper = 0.0, 1.0

    for direction, boundary in (
        (-dx, start[0] - x0),
        (dx, x1 - start[0]),
        (-dy, start[1] - y0),
        (dy, y1 - start[1]),
    ):
        if abs(direction) < 1e-12:
            if boundary < 0:
                return None
            continue
        ratio = boundary / direction
        if direction < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None

    return (
        (start[0] + lower * dx, start[1] + lower * dy),
        (start[0] + upper * dx, start[1] + upper * dy),
    )


# ── Cosmetic injections: these must produce nothing ──────────────────────


@dataclass(slots=True)
class ChangeLineWeight(Injection):
    """Replot with heavier or lighter pens."""

    factor: float = 2.0
    cosmetic = True
    name = "change_line_weight"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        for element in context.elements:
            for op in element.ops:
                if op.operator == "w" and op.operands:
                    op.operands[0] = _num(max(0.05, float(op.operands[0]) * self.factor))
        return []


@dataclass(slots=True)
class ChangeLineType(Injection):
    """Same geometry, dashed instead of solid."""

    pattern: str = "[3 2] 0"
    cosmetic = True
    name = "change_line_type"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        dash = tokenise(f"{self.pattern} d")
        for element in context.elements:
            if element.kind == "path":
                element.ops[:0] = dash
        return []


@dataclass(slots=True)
class SubstituteFont(Injection):
    """The plotting machine did not have the drawing office's font."""

    new_font: str = "Times-Roman"
    cosmetic = True
    name = "substitute_font"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        resources = context.page.get("/Resources")
        fonts = resources.get("/Font") if resources is not None else None
        if fonts is None:
            return []
        for key in list(fonts.keys()):
            fonts[key]["/BaseFont"] = pikepdf.Name("/" + self.new_font)
        return []


@dataclass(slots=True)
class Colourise(Injection):
    """Plot in colour: give strokes a colour so a mono replot differs."""

    colour: tuple[float, float, float] = (0.0, 0.2, 0.8)
    cosmetic = True
    name = "colourise"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        red, green, blue = self.colour
        stroke = tokenise(f"{red:.2f} {green:.2f} {blue:.2f} RG")
        for element in context.elements:
            if element.kind == "path":
                element.ops[:0] = [Op(list(op.operands), op.operator) for op in stroke]
        return []


@dataclass(slots=True)
class PlotMono(Injection):
    """Monochrome replot: every colour operator becomes black."""

    cosmetic = True
    name = "plot_mono"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        for element in context.elements:
            replacement: list[Op] = []
            for op in element.ops:
                if op.operator in {"rg", "sc", "scn"}:
                    replacement.append(Op(["0"], "g"))
                elif op.operator in {"RG", "SC", "SCN"}:
                    replacement.append(Op(["0"], "G"))
                else:
                    replacement.append(op)
            element.ops[:] = replacement
        return []


@dataclass(slots=True)
class AddWatermark(Injection):
    """A big rotated grey `PRELIMINARY` across the sheet."""

    text: str = "PRELIMINARY"
    angle_deg: float = 45.0
    #: Big enough to span the sheet, which is what a real stamp does — and
    #: what makes masking it by its bounding box unacceptable.
    size: float = 200.0
    grey: float = 0.75
    cosmetic = True
    name = "add_watermark"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        angle = math.radians(self.angle_deg)
        cos, sin = math.cos(angle), math.sin(angle)
        width = _GLYPH_WIDTH_EM * self.size * len(self.text)
        centre_x = context.x0 + context.width / 2
        centre_y = context.y0 + context.height / 2
        origin_x = centre_x - (cos * width) / 2
        origin_y = centre_y - (sin * width) / 2
        stream = (
            f"q {self.grey:.2f} g BT /F1 {self.size:.1f} Tf "
            f"{cos:.4f} {sin:.4f} {-sin:.4f} {cos:.4f} {origin_x:.2f} {origin_y:.2f} Tm "
            f"({_escape(self.text)}) Tj ET Q"
        )
        # Drawn *first*, so the drawing overprints it. A real stamp is behind
        # the content or transparent; an opaque one painted on top genuinely
        # erases the lines under it, and the comparison would be right to
        # report them as removed.
        context.elements.insert(0, Element(kind="text", ops=tokenise(stream), text=self.text))
        return []


@dataclass(slots=True)
class ChangeRevisionLetter(Injection):
    """The whole point of ``rev_letter_only``: nothing else on the sheet moves."""

    old_letter: str = "C"
    new_letter: str = "D"
    cosmetic = True
    name = "change_revision_letter"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        # The revision value sits in the title block, in the bottom strip of
        # the sheet. Only replace a match there, so a stray "C" in the
        # drawing is never touched.
        limit = context.y0 + context.height * 0.14
        for element in context.elements:
            if element.kind != "text" or element.text != self.old_letter:
                continue
            if element.bbox is None or element.bbox[1] > limit:
                continue
            for op in element.ops:
                if op.operator == "Tj" and op.operands:
                    op.operands[-1] = f"({_escape(self.new_letter)})"
            element.text = self.new_letter
        return []


@dataclass(slots=True)
class AddLayer(Injection):
    """Create an optional content group and draw a grid of ticks inside it."""

    layer_name: str = "SETTING OUT"
    region: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    cosmetic = True
    name = "add_layer"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        ocg = _ensure_layer(context, self.layer_name)
        x0, y0, x1, y1 = self.region
        parts = [f"/OC /{_layer_key(self.layer_name)} BDC 0.4 w"]
        steps = 8
        for index in range(steps + 1):
            x = x0 + (x1 - x0) * index / steps
            parts.append(f"{x:.2f} {y0:.2f} m {x:.2f} {y1:.2f} l S")
        parts.append("EMC")
        context.elements.append(
            Element(kind="path", ops=tokenise("\n".join(parts)), bbox=self.region)
        )
        _ = ocg
        return []


@dataclass(slots=True)
class ToggleLayer(Injection):
    """Switch an existing optional content group off. One record, not many."""

    layer_name: str = "SETTING OUT"
    cosmetic = True
    name = "toggle_layer"

    def apply(self, context: InjectionContext) -> list[ExpectedChange]:
        root = context.pdf.Root
        properties = root.get("/OCProperties")
        if properties is None:
            return []
        for ocg in properties.get("/OCGs", []):
            if str(ocg.get("/Name", "")) == self.layer_name:
                default = properties["/D"]
                off = list(default.get("/OFF", []))
                off.append(ocg)
                default["/OFF"] = context.pdf.make_indirect(pikepdf.Array(off))
                on = [item for item in default.get("/ON", []) if item.objgen != ocg.objgen]
                default["/ON"] = context.pdf.make_indirect(pikepdf.Array(on))
        return []


def _layer_key(name: str) -> str:
    return "OC" + re.sub(r"[^A-Za-z0-9]", "", name.title())


def _ensure_layer(context: InjectionContext, name: str) -> pikepdf.Object:
    """Create (or find) an OCG and register it on the page's resources."""
    pdf = context.pdf
    root = pdf.Root
    if "/OCProperties" not in root:
        root["/OCProperties"] = pdf.make_indirect(
            pikepdf.Dictionary(
                OCGs=pikepdf.Array([]),
                D=pikepdf.Dictionary(
                    ON=pikepdf.Array([]), OFF=pikepdf.Array([]), Order=pikepdf.Array([])
                ),
            )
        )
    properties = root["/OCProperties"]
    for ocg in properties["/OCGs"]:
        if str(ocg.get("/Name", "")) == name:
            return ocg

    ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String(name)))
    properties["/OCGs"].append(ocg)
    properties["/D"]["/ON"].append(ocg)
    properties["/D"]["/Order"].append(ocg)

    resources = context.page["/Resources"]
    if "/Properties" not in resources:
        resources["/Properties"] = pikepdf.Dictionary()
    resources["/Properties"][pikepdf.Name("/" + _layer_key(name))] = ocg
    return ocg


# ── Running a spec ──────────────────────────────────────────────────────


@dataclass(slots=True)
class ChangeSpec:
    """Injections for one pair.

    ``base`` runs on both sheets — it builds whatever the case needs to exist
    before anything changes (a colour plot, a layer). ``old`` and ``new`` run
    on one side only. Only ``new`` injections contribute expected changes,
    because the comparison reads the old sheet as the reference.
    """

    base: list[Injection] = field(default_factory=list)
    old: list[Injection] = field(default_factory=list)
    new: list[Injection] = field(default_factory=list)
    name: str = "case"


@dataclass(slots=True)
class InjectedPair:
    """Two sheets and the exact truth about what differs between them."""

    old_path: Path
    new_path: Path
    page_index: int
    expected: list[ExpectedChange] = field(default_factory=list)
    cosmetic_applied: list[str] = field(default_factory=list)
    name: str = "case"

    @property
    def is_cosmetic_only(self) -> bool:
        """True when the engine must report zero changes for this pair."""
        return not self.expected

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "old": str(self.old_path),
            "new": str(self.new_path),
            "page_index": self.page_index,
            "expected": [item.as_dict() for item in self.expected],
            "cosmetic_applied": list(self.cosmetic_applied),
        }


def _page_geometry(page: pikepdf.Page) -> tuple[float, float, float, float]:
    box = [float(value) for value in page.mediabox]
    return box[0], box[1], box[2] - box[0], box[3] - box[1]


def _apply(
    source: Path,
    target: Path,
    injections: list[Injection],
    page_index: int,
    *,
    expect_from: int = 0,
) -> list[ExpectedChange]:
    """Copy *source* to *target* applying *injections* to one page.

    ``expect_from`` is the index at which injections start counting as
    expected changes. Injections before it are the shared *base* — they are
    applied to both sheets, so whatever they draw is identical on each side
    and is not a difference. Recording them as expected changes was the bug
    that made the hatch case look like two injected changes plus one, when
    there was only ever one.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if not injections:
        return []

    expected: list[ExpectedChange] = []
    with pikepdf.Pdf.open(target, allow_overwriting_input=True) as pdf:
        page = pdf.pages[page_index]
        stream = pikepdf.Page(page).obj["/Contents"].read_bytes().decode("latin-1")
        elements = parse_elements(tokenise(stream))
        x0, y0, width, height = _page_geometry(pikepdf.Page(page))
        context = InjectionContext(
            elements=elements,
            pdf=pdf,
            page=page.obj if hasattr(page, "obj") else page,
            width=width,
            height=height,
            x0=x0,
            y0=y0,
        )
        for index, injection in enumerate(injections):
            produced = injection.apply(context)
            if index >= expect_from:
                expected.extend(produced)
        body = render(flatten(context.elements)).encode("latin-1")
        page.obj["/Contents"] = pdf.make_stream(body)
        pdf.save(target)
    return expected


def inject_changes(
    source_pdf: str | Path,
    page: int,
    change_spec: ChangeSpec,
    *,
    out_dir: str | Path,
) -> InjectedPair:
    """Build one ground-truth pair from *source_pdf*.

    The old sheet is the source with the ``base`` and ``old`` injections; the
    new sheet is the source with ``base`` and ``new``. The returned expected
    list covers only the genuine injections — a spec made entirely of
    cosmetic ones returns an empty list, which is exactly the pair the phase
    gate is built on.
    """
    source = Path(source_pdf)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    old_path = directory / f"{change_spec.name}_old.pdf"
    new_path = directory / f"{change_spec.name}_new.pdf"

    _apply(source, old_path, [*change_spec.base, *change_spec.old], page)
    expected = _apply(
        source,
        new_path,
        [*change_spec.base, *change_spec.new],
        page,
        expect_from=len(change_spec.base),
    )

    cosmetic = [
        injection.describe()
        for injection in [*change_spec.base, *change_spec.old, *change_spec.new]
        if injection.cosmetic
    ]
    return InjectedPair(
        old_path=old_path,
        new_path=new_path,
        page_index=page,
        expected=expected,
        cosmetic_applied=cosmetic,
        name=change_spec.name,
    )
