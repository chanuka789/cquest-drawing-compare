"""Shared vocabulary for Phase 5 masking.

Masking decides what is never compared. Get it wrong in one direction and
every sheet reports the revision letter, the date and the signature as
changes; get it wrong in the other and a changed general note — often the
most expensive change on the sheet — is silently hidden.

Two conventions hold everything together:

* **Zones live in page fractions, y down.** ``(0, 0)`` is the top-left corner
  of the page box and ``(1, 1)`` the bottom-right, whatever the sheet size,
  whatever the DPI, wherever the media box happens to start. That is what
  makes a zone detected once on a representative sheet applicable to the
  other 142 sheets on the same template, and what lets the same zone mask a
  raster image, a text item list and a vector path list alike.
* **Protected is not the absence of masked.** A protected region is one the
  application deliberately decided to keep comparing — north arrow, scale
  bar, key plan, general notes — and it wins over any mask that overlaps it.
  Without that, a title block detection that runs slightly wide swallows the
  general notes and nobody ever finds out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from engine.extract.text_extractor import PageText, TextItem


class ZoneType(StrEnum):
    """What a zone is, which decides how it is presented and coloured."""

    TITLEBLOCK = "titleblock"
    #: Read it, do not diff it — it often says in words what changed.
    REVISION_TABLE = "revision_table"
    LOGO = "logo"
    WATERMARK = "watermark"
    STAMP = "stamp"
    ANNOTATION = "annotation"
    FRAME = "frame"
    #: Drawn by the user in the mask editor.
    USER = "user"


class ProtectedType(StrEnum):
    """Regions the application keeps, despite sitting near the sheet edge."""

    NORTH_ARROW = "north_arrow"
    SCALE_BAR = "scale_bar"
    KEY_PLAN = "key_plan"
    GENERAL_NOTES = "general_notes"
    USER = "user_protected"


@dataclass(frozen=True, slots=True)
class FracRect:
    """A rectangle in page fractions, y down from the top-left corner."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x0", min(self.x0, self.x1))
        object.__setattr__(self, "x1", max(self.x0, self.x1))
        object.__setattr__(self, "y0", min(self.y0, self.y1))
        object.__setattr__(self, "y1", max(self.y0, self.y1))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0

    def contains(self, fx: float, fy: float) -> bool:
        return self.x0 <= fx <= self.x1 and self.y0 <= fy <= self.y1

    def intersects(self, other: FracRect) -> bool:
        return not (
            other.x0 > self.x1 or other.x1 < self.x0 or other.y0 > self.y1 or other.y1 < self.y0
        )

    def intersection_area(self, other: FracRect) -> float:
        dx = min(self.x1, other.x1) - max(self.x0, other.x0)
        dy = min(self.y1, other.y1) - max(self.y0, other.y0)
        return dx * dy if dx > 0 and dy > 0 else 0.0

    def iou(self, other: FracRect) -> float:
        intersection = self.intersection_area(other)
        if intersection <= 0:
            return 0.0
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def expanded(self, margin: float) -> FracRect:
        return FracRect(self.x0 - margin, self.y0 - margin, self.x1 + margin, self.y1 + margin)

    def clipped(self) -> FracRect:
        """Clamped to the page. A zone can be dragged past the edge."""
        return FracRect(
            min(max(self.x0, 0.0), 1.0),
            min(max(self.y0, 0.0), 1.0),
            min(max(self.x1, 0.0), 1.0),
            min(max(self.y1, 0.0), 1.0),
        )

    def union(self, other: FracRect) -> FracRect:
        return FracRect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def to_px(self, width_px: float, height_px: float) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) in image pixels, y down."""
        return (self.x0 * width_px, self.y0 * height_px, self.x1 * width_px, self.y1 * height_px)

    def as_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}

    @staticmethod
    def from_points(points: Iterable[tuple[float, float]]) -> FracRect:
        xs: list[float] = []
        ys: list[float] = []
        for x, y in points:
            xs.append(float(x))
            ys.append(float(y))
        if not xs:
            return FracRect(0.0, 0.0, 0.0, 0.0)
        return FracRect(min(xs), min(ys), max(xs), max(ys))


@dataclass(slots=True)
class Zone:
    """One excluded region, with the evidence for why it was excluded."""

    type: ZoneType
    rect: FracRect
    #: An irregular shape, when a rectangle is not enough. Page fractions.
    polygon: list[tuple[float, float]] = field(default_factory=list)
    label: str = ""
    confidence: float = 1.0
    #: Human-readable evidence, so the mask editor can explain the decision.
    evidence: list[str] = field(default_factory=list)
    enabled: bool = True
    #: True when a user created or edited it; those are never re-detected over.
    user_edited: bool = False
    #: Exclude only this zone's own ink, not everything inside its box.
    #:
    #: A 45-degree `PRELIMINARY` across an A1 sheet has a bounding box
    #: covering a third of the drawing. Masking that box would hide a third
    #: of the sheet from comparison and nobody would ever be told. A pale
    #: watermark can instead be removed by *value*: inside the box, pixels
    #: lighter than :attr:`ink_threshold` are the watermark and the solid
    #: black lines under it are the drawing, which stays compared.
    ink_only: bool = False
    #: Grey value (0 black, 255 white) above which ink here is the watermark.
    ink_threshold: int = 130
    #: The normalised string this zone suppresses, for the text stream.
    match_text: str = ""

    @property
    def needs_confirmation(self) -> bool:
        """Below this, the zone is proposed rather than applied silently."""
        return self.confidence < 0.7

    def contains(self, fx: float, fy: float) -> bool:
        """Whether this zone excludes a point outright.

        An ink-only zone never excludes a point on its own: it has no opinion
        about the drawing under it, only about its own pale ink.
        """
        if not self.enabled or self.ink_only:
            return False
        if self.polygon:
            return _point_in_polygon(fx, fy, self.polygon)
        return self.rect.contains(fx, fy)

    def suppresses_text(self, fx: float, fy: float, normalised_text: str) -> bool:
        """Whether this zone excludes one piece of text.

        For an ink-only zone the box must contain the text *and* the string
        must be the watermark's own — so `PRELIMINARY` is dropped while the
        room name it was stamped across is kept.
        """
        if not self.enabled:
            return False
        if not self.ink_only:
            return self.contains(fx, fy)
        if not self.rect.contains(fx, fy) or len(self.match_text) < 3:
            return False
        # One direction only. Testing the reverse as well ("is this item a
        # piece of the watermark?") drops `W3` because it happens to be a
        # substring of `W3 FFL + PRELIMINARY`, which is a real tag lost for a
        # spelling coincidence.
        return self.match_text in normalised_text

    def clean_text(self, fx: float, fy: float, text: str) -> str:
        """Strip this watermark's stray glyphs out of a piece of drawing text.

        A PDF reader reports the text inside a run's box, and a watermark set
        at 200 pt has glyphs large enough to overlap the labels beneath it —
        so `RM-02` comes back as `RM-02 P`, and the comparison reports a tag
        that never changed. Only single letters that genuinely belong to this
        watermark's phrase are removed, and only inside its box.
        """
        if not self.ink_only or len(self.match_text) < 3:
            return text
        if not self.rect.contains(fx, fy):
            return text

        tokens = text.split()
        if len(tokens) < 2:
            return text
        kept = [
            token for token in tokens if not (len(token) <= 2 and token.upper() in self.match_text)
        ]
        return " ".join(kept) if kept else text

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "rect": self.rect.as_dict(),
            "polygon": [list(point) for point in self.polygon],
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
            "enabled": self.enabled,
            "user_edited": self.user_edited,
            "needs_confirmation": self.needs_confirmation,
            "ink_only": self.ink_only,
            "ink_threshold": self.ink_threshold,
            "match_text": self.match_text,
        }


@dataclass(slots=True)
class ProtectedRegion:
    """A region the application deliberately keeps comparing."""

    type: ProtectedType
    rect: FracRect
    label: str = ""
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "rect": self.rect.as_dict(),
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
        }


def _point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting. Points exactly on an edge count as inside."""
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x <= crossing:
                inside = not inside
    return inside


# ── The sheet a detector looks at ───────────────────────────────────────


@dataclass(slots=True)
class SheetView:
    """One sheet, as every Phase 5 detector wants to see it.

    Text arrives in PDF user-space points and pixels in image space; this
    holds both plus the conversions, so no detector re-derives them and no
    detector has to know where the media box started.
    """

    #: Extracted text with coordinates. None on a scanned sheet.
    page_text: PageText | None = None
    #: Grayscale render at :attr:`dpi`, y down. None when only text is needed.
    gray: np.ndarray | None = None
    width_px: int = 0
    height_px: int = 0
    dpi: int = 200
    #: Drawing scale denominator from the title block, when it was read.
    scale_denominator: int | None = None
    scale_text: str | None = None
    #: Where it came from, for logs and the run record.
    source_path: str = ""
    page_index: int = 0

    def __post_init__(self) -> None:
        if self.gray is not None and not self.width_px:
            self.height_px, self.width_px = int(self.gray.shape[0]), int(self.gray.shape[1])

    @property
    def px_per_mm(self) -> float:
        return self.dpi / 25.4

    @property
    def has_text(self) -> bool:
        return self.page_text is not None and not self.page_text.is_empty

    def item_rect(self, item: TextItem) -> FracRect:
        """One text item's box in page fractions, y down.

        :class:`~engine.extract.text_extractor.TextItem` reports y up from the
        page box origin; the flip happens here and nowhere else.
        """
        page = self.page_text
        if page is None or page.box.width <= 0 or page.box.height <= 0:
            return FracRect(0.0, 0.0, 0.0, 0.0)
        box = page.box
        x0 = (item.x - box.x0) / box.width
        x1 = (item.right - box.x0) / box.width
        # y up -> y down: the item's top edge becomes the smaller fraction.
        y0 = 1.0 - (item.top - box.y0) / box.height
        y1 = 1.0 - (item.y - box.y0) / box.height
        return FracRect(x0, y0, x1, y1)

    def item_centre(self, item: TextItem) -> tuple[float, float]:
        return self.item_rect(item).centre

    def px_rect(self, rect: FracRect) -> tuple[int, int, int, int]:
        """A fractional rect as integer pixel bounds, clipped to the image."""
        x0, y0, x1, y1 = rect.clipped().to_px(self.width_px, self.height_px)
        return (
            int(np.floor(x0)),
            int(np.floor(y0)),
            int(np.ceil(x1)),
            int(np.ceil(y1)),
        )

    def fraction_of_px(self, x_px: float, y_px: float) -> tuple[float, float]:
        fx = x_px / self.width_px if self.width_px else 0.0
        fy = y_px / self.height_px if self.height_px else 0.0
        return fx, fy
