"""Task 5.8 (normalisation) — making two drawings of the same line equal.

This is the step that decides whether vector comparison works at all. The
same wall, plotted from the same model on two different days, can arrive as:

* two Béziers with different control points that render identically,
* one line, or the same line drawn as three collinear segments,
* a path from A to B, or the same path from B to A,
* coordinates differing in the fourth decimal place,
* the same geometry with a heavier pen and a dashed line type.

Every one of those must hash to the same geometry. If they do not, a
re-plotted sheet reports every object on it as removed and re-added, which is
the worst output the tool can produce.

The five steps, in order, are flatten, merge, canonicalise, quantise, and
split style from geometry. The last one is what makes a line weight change
reportable as cosmetic instead of as ten thousand changed lines.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from engine.compare.types import Bbox
from engine.extract.vector_extractor import SEGMENT_BEZIERTO, SEGMENT_MOVETO, RawPath

#: Curves are flattened to this accuracy, in millimetres on paper. Two curves
#: that render the same must flatten to the same points, and at 0.1 mm no
#: plotter and no eye can tell the difference.
FLATTEN_TOLERANCE_PAPER_MM = 0.1
#: Two joined segments merge when their directions differ by less than this.
COLLINEAR_TOLERANCE_DEG = 0.5
#: Hard cap on subdivision depth, so a pathological curve cannot hang a run.
MAX_BEZIER_DEPTH = 10


Point = tuple[float, float]


@dataclass(slots=True)
class NormalisedPath:
    """One path reduced to a comparable form."""

    points: list[Point] = field(default_factory=list)
    closed: bool = False
    geometry_hash: str = ""
    style_hash: str = ""
    bbox: Bbox = field(default_factory=lambda: Bbox(0.0, 0.0, 0.0, 0.0))
    #: Total length in image pixels.
    length: float = 0.0
    point_count: int = 0
    geometry_type: str = "polyline"
    line_width_px: float = 0.0
    dash_array: tuple[float, ...] = ()
    stroke_colour: tuple[int, int, int, int] = (0, 0, 0, 255)
    in_optional_content: bool = False
    #: Index of the raw path this came from, for reporting.
    source_index: int = -1

    @property
    def centre_px(self) -> tuple[float, float]:
        return self.bbox.centre

    @property
    def is_short(self) -> bool:
        """Short segments are hatch candidates; see :mod:`engine.compare.hatch`."""
        return self.point_count == 2


# ── Flattening ──────────────────────────────────────────────────────────


def flatten_cubic(
    p0: Point, p1: Point, p2: Point, p3: Point, tolerance: float, depth: int = 0
) -> list[Point]:
    """Subdivide one cubic Bézier until it is flat to *tolerance* pixels.

    Recursion is on flatness, not on a fixed step count: a nearly straight
    curve becomes two points and a tight one becomes as many as it needs, so
    two curves that render identically flatten identically.
    """
    if depth >= MAX_BEZIER_DEPTH or _is_flat(p0, p1, p2, p3, tolerance):
        return [p3]

    # de Casteljau split at t = 0.5.
    p01 = _midpoint(p0, p1)
    p12 = _midpoint(p1, p2)
    p23 = _midpoint(p2, p3)
    p012 = _midpoint(p01, p12)
    p123 = _midpoint(p12, p23)
    mid = _midpoint(p012, p123)

    left = flatten_cubic(p0, p01, p012, mid, tolerance, depth + 1)
    right = flatten_cubic(mid, p123, p23, p3, tolerance, depth + 1)
    return left + right


def _midpoint(first: Point, second: Point) -> Point:
    return ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)


def _is_flat(p0: Point, p1: Point, p2: Point, p3: Point, tolerance: float) -> bool:
    """Both control points within *tolerance* of the chord."""
    return (
        _point_line_distance(p1, p0, p3) <= tolerance
        and _point_line_distance(p2, p0, p3) <= tolerance
    )


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    cross = abs(dx * (start[1] - point[1]) - (start[0] - point[0]) * dy)
    return cross / length


def flatten_path(path: RawPath, tolerance_px: float) -> list[tuple[list[Point], bool]]:
    """Turn a raw path into polylines: (points, closed) per subpath."""
    subpaths: list[tuple[list[Point], bool]] = []
    current: list[Point] = []
    closed = False

    def flush() -> None:
        nonlocal current, closed
        if len(current) >= 2:
            subpaths.append((current, closed))
        current = []
        closed = False

    for segment in path.segments:
        if segment.kind == SEGMENT_MOVETO:
            flush()
            current = list(segment.points[:1])
            continue
        if segment.kind == SEGMENT_BEZIERTO and len(segment.points) == 3 and current:
            control1, control2, end = segment.points
            current.extend(flatten_cubic(current[-1], control1, control2, end, tolerance_px))
        else:
            current.extend(segment.points)
        if segment.closes:
            closed = True

    flush()
    return subpaths


# ── Merging, canonicalising, quantising ─────────────────────────────────


def merge_collinear(
    points: list[Point], tolerance_deg: float = COLLINEAR_TOLERANCE_DEG
) -> list[Point]:
    """Drop interior points that lie on the line between their neighbours.

    One wall drawn as three segments must equal the same wall drawn as one,
    or a re-plot with a different segment count reports as a change.
    """
    if len(points) < 3:
        return list(points)

    tolerance = math.radians(tolerance_deg)
    merged: list[Point] = [points[0]]
    for index in range(1, len(points) - 1):
        previous = merged[-1]
        current = points[index]
        following = points[index + 1]
        first = math.atan2(current[1] - previous[1], current[0] - previous[0])
        second = math.atan2(following[1] - current[1], following[0] - current[0])
        difference = abs(_wrap_angle(second - first))
        if difference > tolerance:
            merged.append(current)
    merged.append(points[-1])
    return merged


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def quantise(points: list[Point], grid: float) -> list[Point]:
    """Snap coordinates to the tolerance grid before hashing."""
    if grid <= 0:
        return list(points)
    return [(round(x / grid) * grid, round(y / grid) * grid) for x, y in points]


def canonicalise(points: list[Point], closed: bool) -> list[Point]:
    """A deterministic start point and direction.

    A path and its reverse are the same geometry and must hash identically.
    For an open path the two candidates are the sequence and its reverse; the
    lexicographically smaller one wins. For a closed path every rotation is
    also a candidate, so it is rotated to start at its smallest point first.
    """
    if len(points) < 2:
        return list(points)

    if closed:
        ring = points[:-1] if points[0] == points[-1] else list(points)
        if not ring:
            return list(points)
        start = min(range(len(ring)), key=lambda index: ring[index])
        rotated = ring[start:] + ring[:start]
        reversed_ring = list(reversed(rotated))
        # Keep the first point fixed when reversing a ring.
        reversed_ring = [rotated[0], *reversed_ring[:-1]]
        chosen = min(rotated, reversed_ring)
        return [*chosen, chosen[0]]

    forward = list(points)
    backward = list(reversed(points))
    return min(forward, backward)


# ── Classification and hashing ──────────────────────────────────────────


def classify_geometry(points: list[Point], closed: bool, from_curve: bool) -> str:
    """Name the shape, for the report: line, rectangle, circle, polyline."""
    unique = points[:-1] if closed and len(points) > 1 and points[0] == points[-1] else points
    count = len(unique)
    if count == 2:
        return "line"
    if closed and count == 4 and _is_rectangle(unique):
        return "rectangle"
    if from_curve and closed and _is_circular(unique):
        return "circle"
    if from_curve:
        return "arc"
    return "polyline"


def _is_rectangle(points: list[Point]) -> bool:
    for index in range(4):
        a = points[index]
        b = points[(index + 1) % 4]
        c = points[(index + 2) % 4]
        first = (b[0] - a[0], b[1] - a[1])
        second = (c[0] - b[0], c[1] - b[1])
        dot = first[0] * second[0] + first[1] * second[1]
        magnitude = math.hypot(*first) * math.hypot(*second)
        if magnitude < 1e-9 or abs(dot) / magnitude > 0.02:
            return False
    return True


def _is_circular(points: list[Point]) -> bool:
    if len(points) < 6:
        return False
    centre_x = sum(point[0] for point in points) / len(points)
    centre_y = sum(point[1] for point in points) / len(points)
    radii = [math.hypot(point[0] - centre_x, point[1] - centre_y) for point in points]
    mean = sum(radii) / len(radii)
    if mean < 1e-6:
        return False
    spread = max(abs(radius - mean) for radius in radii) / mean
    return spread < 0.08


def path_length(points: list[Point]) -> float:
    return sum(
        math.hypot(points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1])
        for index in range(len(points) - 1)
    )


def geometry_hash(points: list[Point], closed: bool, grid: float) -> str:
    """A hash of the geometry alone. Style is deliberately not in it."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(b"C" if closed else b"O")
    decimals = max(0, round(-math.log10(grid))) + 1 if grid > 0 else 3
    for x, y in points:
        digest.update(f"{x:.{decimals}f},{y:.{decimals}f};".encode())
    return digest.hexdigest()


def style_hash(path: RawPath) -> str:
    """A hash of the graphics state: pen, dash pattern, colour."""
    digest = hashlib.blake2b(digest_size=8)
    digest.update(f"w={path.line_width_px:.2f};".encode())
    digest.update(f"d={','.join(f'{value:.2f}' for value in path.dash_array)};".encode())
    digest.update(f"s={path.stroke_colour};f={path.fill_colour};".encode())
    return digest.hexdigest()


# ── The entry point ─────────────────────────────────────────────────────


def normalise_path(
    path: RawPath,
    *,
    tolerance_px: float,
    px_per_mm: float,
    source_index: int = -1,
) -> list[NormalisedPath]:
    """Normalise one raw path into comparable subpaths.

    Returns a list because one PDF path object can hold several subpaths —
    a hatch is often one object containing two thousand of them. Degenerate
    subpaths (zero length, single point) are dropped: they draw nothing, and
    keeping them would make two identical sheets differ.
    """
    flatten_tolerance = max(FLATTEN_TOLERANCE_PAPER_MM * px_per_mm, 0.05)
    grid = max(tolerance_px, 1e-3)
    from_curve = any(segment.kind == SEGMENT_BEZIERTO for segment in path.segments)

    results: list[NormalisedPath] = []
    for points, closed in flatten_path(path, flatten_tolerance):
        snapped = quantise(points, grid)
        merged = merge_collinear(snapped)
        if closed and len(merged) > 1 and merged[0] != merged[-1]:
            merged.append(merged[0])
        canonical = canonicalise(merged, closed)
        if len(canonical) < 2:
            continue
        length = path_length(canonical)
        if length <= grid:
            continue  # a point, not a line

        results.append(
            NormalisedPath(
                points=canonical,
                closed=closed,
                geometry_hash=geometry_hash(canonical, closed, grid),
                style_hash=style_hash(path),
                bbox=Bbox.from_points(canonical),
                length=length,
                point_count=len(canonical),
                geometry_type=classify_geometry(canonical, closed, from_curve),
                line_width_px=path.line_width_px,
                dash_array=path.dash_array,
                stroke_colour=path.stroke_colour,
                in_optional_content=path.in_optional_content,
                source_index=source_index,
            )
        )
    return results


def normalise_paths(
    paths: list[RawPath], *, tolerance_px: float, px_per_mm: float
) -> list[NormalisedPath]:
    """Normalise a whole page's geometry."""
    output: list[NormalisedPath] = []
    for index, path in enumerate(paths):
        if path.is_degenerate:
            continue
        output.extend(
            normalise_path(path, tolerance_px=tolerance_px, px_per_mm=px_per_mm, source_index=index)
        )
    return output


def transform_points(points: list[Point], matrix: list[list[float]]) -> list[Point]:
    """Map points through the Phase 4 alignment matrix (old px -> new px)."""
    result: list[Point] = []
    for x, y in points:
        denominator = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2]
        if abs(denominator) < 1e-12:
            denominator = 1.0
        result.append(
            (
                (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]) / denominator,
                (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]) / denominator,
            )
        )
    return result
