"""Shared vocabulary for Phase 5 comparison.

Every stream — text, vector, hatch, raster — produces its own findings, and
the orchestrator merges them into one list. They can only merge if they agree
on three things, which is what this module fixes:

* **One coordinate space.** Everything is expressed in *new-sheet image
  pixels at the comparison DPI*, y down. The old sheet's items arrive in its
  own space and are warped by the Phase 4 alignment matrix before anything is
  compared. Text and vector geometry come out of the PDF in user-space points
  (y up, arbitrary box origin) and are converted once, by
  :func:`engine.align.anchors.page_points_to_image_px`.
* **Pixels are internal, millimetres are public.** The same rule as Phase 4.
  A :class:`ChangeRecord` carries its bounding box in pixels and the
  conversion factors beside it, so anything crossing the API boundary can
  report millimetres on paper and millimetres on site without guessing.
* **Nothing is discarded silently.** A filter marks a record cosmetic or
  moves it to the filtered list with a reason. The user can always ask what
  was hidden and why — that is what makes a short report trustworthy.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Stream(StrEnum):
    """Which comparison produced a finding.

    Ordered by how much a finding from it is worth: a text change is the
    highest-value output on a construction drawing, a raster difference the
    least specific.
    """

    TEXT = "text"
    VECTOR = "vector"
    HATCH = "hatch"
    RASTER = "raster"

    @property
    def rank(self) -> int:
        order = {Stream.TEXT: 0, Stream.HATCH: 1, Stream.VECTOR: 2, Stream.RASTER: 3}
        return order[self]


class ChangeKind(StrEnum):
    """What happened. Deliberately more specific than the Phase 1 ChangeType.

    The coarse `ChangeType` stored in the database (added/removed/moved/
    modified/cosmetic) is what the register speaks; these are what the
    comparison engine speaks, and :meth:`coarse` maps one onto the other.
    """

    ADDED = "added"
    REMOVED = "removed"
    MOVED = "moved"
    MODIFIED = "modified"
    #: Same geometry, different line weight, dash pattern or colour.
    STYLE_ONLY = "style_only"
    #: A hatched region whose pattern changed — blockwork to concrete.
    HATCH_PATTERN_CHANGED = "hatch_pattern_changed"
    HATCH_AREA_CHANGED = "hatch_area_changed"
    HATCH_ADDED = "hatch_added"
    HATCH_REMOVED = "hatch_removed"
    #: Many tags renumbered by one consistent mapping.
    TAGS_RENUMBERED = "tags_renumbered"
    #: A whole PDF optional content group appeared or disappeared.
    LAYER_VISIBILITY_CHANGED = "layer_visibility_changed"
    #: One large coherent region redrawn — an xref or background update.
    BACKGROUND_UPDATED = "background_updated"

    def coarse(self) -> str:
        """The `core.enums.ChangeType` value this maps onto."""
        if self in {ChangeKind.ADDED, ChangeKind.HATCH_ADDED}:
            return "added"
        if self in {ChangeKind.REMOVED, ChangeKind.HATCH_REMOVED}:
            return "removed"
        if self is ChangeKind.MOVED:
            return "moved"
        if self is ChangeKind.STYLE_ONLY:
            return "cosmetic"
        return "modified"


class TextCategory(StrEnum):
    """What a piece of text on a drawing *is*.

    Classification drives two things: how a change is described in words, and
    how much it is likely to be worth. A dimension changing costs money; a
    grid letter changing almost never does.
    """

    DIMENSION = "dimension"
    LEVEL = "level"
    TAG = "tag"
    ROOM = "room"
    SCALE = "scale"
    GRID = "grid"
    NOTE = "note"
    SPEC = "spec"
    TITLEBLOCK = "titleblock"
    UNKNOWN = "unknown"


class CrossCheckFlag(StrEnum):
    """The two dimension-versus-geometry traps from the plan (B4 step 5)."""

    #: The dimension text changed but nothing moved near it. Either the
    #: drawing was wrong before, or the item is marked not to scale.
    DIMENSION_TEXT_ONLY = "dimension_text_only"
    #: Geometry moved but the dimension beside it still reads the old value.
    #: Usually a drafting error, and worth raising.
    GEOMETRY_MOVED_DIMENSION_STALE = "geometry_moved_dimension_stale"
    #: Text and geometry agree — the normal, healthy case.
    CONSISTENT = "consistent"
    #: Not enough geometry nearby to say anything honest.
    UNKNOWN = "unknown"


# ── Geometry ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Bbox:
    """An axis-aligned box in new-sheet image pixels, y down."""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def centre(self) -> tuple[float, float]:
        return self.cx, self.cy

    def expanded(self, margin: float) -> Bbox:
        """The same box grown by *margin* on every side."""
        return Bbox(self.x - margin, self.y - margin, self.w + 2 * margin, self.h + 2 * margin)

    def contains_point(self, x: float, y: float) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def intersects(self, other: Bbox) -> bool:
        return not (
            other.x > self.right
            or other.right < self.x
            or other.y > self.bottom
            or other.bottom < self.y
        )

    def intersection_area(self, other: Bbox) -> float:
        dx = min(self.right, other.right) - max(self.x, other.x)
        dy = min(self.bottom, other.bottom) - max(self.y, other.y)
        if dx <= 0 or dy <= 0:
            return 0.0
        return dx * dy

    def iou(self, other: Bbox) -> float:
        """Intersection over union. 0 when they do not overlap at all."""
        intersection = self.intersection_area(other)
        if intersection <= 0:
            return 0.0
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def union(self, other: Bbox) -> Bbox:
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Bbox(x, y, max(self.right, other.right) - x, max(self.bottom, other.bottom) - y)

    def to_mm(self, px_per_mm: float) -> tuple[float, float, float, float]:
        """(x, y, w, h) in millimetres on paper. Never shown as pixels."""
        if px_per_mm <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (self.x / px_per_mm, self.y / px_per_mm, self.w / px_per_mm, self.h / px_per_mm)

    @staticmethod
    def from_points(points: Iterable[tuple[float, float]]) -> Bbox:
        """The tight box around a set of points; a degenerate box if empty."""
        xs: list[float] = []
        ys: list[float] = []
        for x, y in points:
            xs.append(float(x))
            ys.append(float(y))
        if not xs:
            return Bbox(0.0, 0.0, 0.0, 0.0)
        return Bbox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @staticmethod
    def from_corners(x0: float, y0: float, x1: float, y1: float) -> Bbox:
        return Bbox(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def union_all(boxes: Sequence[Bbox]) -> Bbox:
    """One box around all of them. A degenerate box when the list is empty."""
    if not boxes:
        return Bbox(0.0, 0.0, 0.0, 0.0)
    result = boxes[0]
    for box in boxes[1:]:
        result = result.union(box)
    return result


# ── Per-stream detail carried by a change ────────────────────────────────


@dataclass(slots=True)
class TextChangeDetail:
    """Everything the text stream knows about one change."""

    category: TextCategory = TextCategory.UNKNOWN
    old_text: str | None = None
    new_text: str | None = None
    old_position: tuple[float, float] | None = None
    new_position: tuple[float, float] | None = None
    #: How far it moved, in millimetres on site when the scale is known.
    distance_moved_site_mm: float | None = None
    distance_moved_paper_mm: float | None = None
    similarity: float = 0.0
    #: Numeric analysis, when both sides parse as numbers.
    numeric_delta: float | None = None
    percent_delta: float | None = None
    site_delta_mm: float | None = None
    cross_check: CrossCheckFlag = CrossCheckFlag.UNKNOWN


@dataclass(slots=True)
class HatchChangeDetail:
    """Everything the hatch stream knows about one region change."""

    old_signature: str | None = None
    new_signature: str | None = None
    old_angle_deg: float | None = None
    new_angle_deg: float | None = None
    old_spacing_paper_mm: float | None = None
    new_spacing_paper_mm: float | None = None
    old_area_m2: float | None = None
    new_area_m2: float | None = None
    area_delta_m2: float | None = None
    segment_count: int = 0


@dataclass(slots=True)
class VectorChangeDetail:
    """Everything the vector stream knows about one change."""

    path_count: int = 0
    total_length_site_mm: float | None = None
    total_length_paper_mm: float = 0.0
    geometry_type: str = "polyline"
    #: Set when the geometry matched and only the graphics state differed.
    style_difference: str = ""
    layer: str | None = None


# ── The change record ───────────────────────────────────────────────────


@dataclass(slots=True)
class ChangeRecord:
    """One finding, in new-sheet image pixels, ready to merge across streams.

    A record is never deleted by a filter. It is either marked cosmetic (kept,
    hidden by default) or moved to the run's filtered list with a reason.
    """

    kind: ChangeKind
    bbox: Bbox
    #: Which streams found it. Two streams agreeing raises confidence.
    streams: list[Stream] = field(default_factory=list)
    description: str = ""
    confidence: float = 0.5
    is_cosmetic: bool = False
    #: Populated by whichever stream produced the record.
    text: TextChangeDetail | None = None
    hatch: HatchChangeDetail | None = None
    vector: VectorChangeDetail | None = None
    #: Free-form evidence for the debug view; never required to be readable.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        """A single word for grouping: the text category or the stream."""
        if self.text is not None:
            return str(self.text.category)
        if self.hatch is not None:
            return "hatch"
        if self.vector is not None:
            return self.vector.geometry_type
        return "region"

    @property
    def primary_stream(self) -> Stream:
        """The most valuable stream that found it."""
        if not self.streams:
            return Stream.RASTER
        return min(self.streams, key=lambda stream: stream.rank)

    def with_stream(self, stream: Stream) -> ChangeRecord:
        if stream not in self.streams:
            self.streams.append(stream)
        return self


@dataclass(slots=True)
class FilteredChange:
    """A change a noise filter removed, kept so the user can ask why."""

    change: ChangeRecord
    #: Which filter removed it, e.g. "filter_speckle".
    filter_name: str
    #: One sentence, written for a user: why this was not worth reporting.
    reason: str


@dataclass(slots=True)
class StreamStats:
    """What one stream did, for the run record and the debug view."""

    stream: Stream
    ran: bool = False
    #: Why it did not run, when it did not. Never a silent skip.
    skip_reason: str = ""
    old_item_count: int = 0
    new_item_count: int = 0
    change_count: int = 0
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stream": str(self.stream),
            "ran": self.ran,
            "skip_reason": self.skip_reason,
            "old_item_count": self.old_item_count,
            "new_item_count": self.new_item_count,
            "change_count": self.change_count,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass(slots=True)
class Warning_:
    """A sheet-level message that is more useful than a change list.

    The alignment-residual case is the reason this exists: telling the user
    "this sheet aligned to 2.8 mm, which is not tight enough at 1:50" is
    honest and actionable, and reporting four hundred halo fragments is not.
    """

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}
