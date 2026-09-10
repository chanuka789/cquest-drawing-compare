"""Vocabulary for Stage 6: the change regions found on one aligned pair.

Everything the user ever sees is expressed in millimetres - on paper and,
where the drawing scale is known, at real-world scale. Pixels appear only
in :attr:`ChangeRegion.bbox_px`, which exists so the viewer can draw the
box on the sheet; it is never shown as a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.enums import ChangeType, Severity

#: Ink tolerance: how far a stroke may drift and still count as unchanged.
#: Expressed on paper, because that is where residual alignment error lives.
DEFAULT_TOLERANCE_MM = 0.35

#: Clusters smaller than this on paper are noise, not changes.
DEFAULT_MIN_AREA_MM2 = 1.0

#: Clusters closer than this on paper are one change, not two.
DEFAULT_MERGE_GAP_MM = 3.0


@dataclass(slots=True)
class CompareConfig:
    """Every knob Stage 6 exposes, with its defaults.

    The DPI default matches the Phase 4 comparison DPI. It is never
    hard-coded at a call site - pass it through from the caller.
    """

    dpi: int = 200
    #: Ink drift allowed before a stroke counts as changed (paper mm).
    tolerance_mm: float = DEFAULT_TOLERANCE_MM
    #: Smallest cluster reported (paper mm²).
    min_area_mm2: float = DEFAULT_MIN_AREA_MM2
    #: Clusters within this distance merge into one region (paper mm).
    merge_gap_mm: float = DEFAULT_MERGE_GAP_MM
    #: Ignore a margin this wide around the sheet edge, where warping the
    #: old sheet onto the new grid leaves partial coverage (paper mm).
    edge_margin_mm: float = 2.0
    #: A removed cluster that reappears elsewhere at this correlation or
    #: better is reported as MOVED rather than as a removal plus an addition.
    move_correlation: float = 0.85
    #: How far a cluster may travel and still be called a move (paper mm).
    move_search_mm: float = 120.0
    #: Regions at or below this area are flagged cosmetic (paper mm²).
    cosmetic_area_mm2: float = 4.0
    #: Cap on reported regions. Beyond this the pair is over-changed and the
    #: list stops being readable; the caller is told it was truncated.
    max_regions: int = 500


@dataclass(slots=True)
class ChangeRegion:
    """One rectangle of the new sheet that differs from the old one."""

    #: Stable within a single compare run, so the UI can key rows.
    index: int
    change_type: ChangeType
    severity: Severity
    #: Bounding box on the *new* sheet's pixel grid: x, y, width, height.
    bbox_px: tuple[int, int, int, int]
    #: The same box on paper, in millimetres: x, y, width, height.
    bbox_mm: tuple[float, float, float, float]
    #: Area of changed ink (not of the box) on paper.
    area_mm2: float
    #: The same area at drawing scale, when the scale could be read.
    area_site_mm2: float | None
    #: Changed ink pixels that are new, and that are gone.
    added_px: int
    removed_px: int
    #: True when the region is small enough to be presentation-only.
    is_cosmetic: bool
    #: Where a MOVED region came from, on the new sheet's grid.
    moved_from_px: tuple[int, int, int, int] | None = None
    #: How far it moved, on paper and at drawing scale.
    moved_by_mm: float | None = None
    moved_by_site_mm: float | None = None
    #: What the region said before and after, when the text layer could be
    #: read and the words differ. A dimension edit is a few dozen pixels
    #: and can change a whole wall, so this - not area - drives severity.
    text_kind: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    #: One sentence the user can read without knowing how any of this works.
    explanation: str = ""


@dataclass(slots=True)
class CompareResult:
    """Everything one compared pair produced."""

    old_path: str
    new_path: str
    old_page_index: int
    new_page_index: int
    dpi: int
    #: The new sheet's pixel grid, which every bbox refers to.
    width_px: int
    height_px: int
    #: Drawing scale denominator, when it could be read (1:100 -> 100).
    scale_denominator: int | None
    regions: list[ChangeRegion] = field(default_factory=list)
    #: True when `max_regions` cut the list short.
    truncated: bool = False
    #: Total changed ink, before clustering dropped anything.
    total_added_px: int = 0
    total_removed_px: int = 0
    duration_s: float = 0.0
    #: Set when the pair could not be compared at all. Regions is then empty.
    failure: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        """Regions per change type, for the summary line."""
        tally: dict[str, int] = {}
        for region in self.regions:
            key = str(region.change_type)
            tally[key] = tally.get(key, 0) + 1
        return tally

    @property
    def substantive_count(self) -> int:
        """Regions that are not merely cosmetic."""
        return sum(1 for region in self.regions if not region.is_cosmetic)
