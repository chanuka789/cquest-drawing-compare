"""Task 5.13 — tolerance, expressed the way a quantity surveyor thinks.

A user says "ignore anything that moved less than 25 millimetres on site".
The comparison code needs pixels. The conversion runs one way only:

    site mm  ->  / scale denominator  ->  paper mm  ->  x px_per_mm  ->  px

At 1:100 and 200 DPI, 50 mm on site is 0.5 mm on paper is 3.9 px.

**The clamp is the part that matters.** A 1:5 detail sheet with a 25 mm site
tolerance resolves to 5 mm on paper, which is enormous — it would hide nearly
every change on the sheet. A 1:500 site plan resolves to 0.05 mm, smaller than
one pixel at any sane DPI, so nothing would ever match and the whole sheet
would read as changed. Both failures are silent and both destroy trust, so the
paper result is clamped to a sane band and the fact that it was clamped is
reported, never hidden.

Nothing here ever shows a user a pixel count: :func:`format_for_user` always
says both millimetre figures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from engine.align.units import px_per_mm as px_per_mm_at
from engine.titleblock.scale_reader import read_scale
from engine.utils.errors import ValidationError

#: Comparison DPI default. Never hard-code this anywhere else — pass the
#: resolved tolerance around instead.
DEFAULT_COMPARISON_DPI = 200


@dataclass(slots=True)
class ToleranceSpec:
    """What the user asked for, in the units the user thinks in."""

    #: How far something may move before it counts as a change (site mm).
    position_site_mm: float = 25.0
    #: How small a change region may be before it is ignored (site mm).
    min_region_site_mm: float = 50.0
    #: The clamp band applied to the converted paper figure.
    paper_min_mm: float = 0.2
    paper_max_mm: float = 3.0
    #: Used when the sheet has no usable scale (NTS, details, unparsed).
    fallback_position_paper_mm: float = 0.5
    fallback_min_region_paper_mm: float = 1.0
    dpi: int = DEFAULT_COMPARISON_DPI

    def __post_init__(self) -> None:
        if self.position_site_mm <= 0 or self.min_region_site_mm <= 0:
            raise ValidationError(
                "Tolerances must be greater than zero millimetres.",
                detail={
                    "position_site_mm": self.position_site_mm,
                    "min_region_site_mm": self.min_region_site_mm,
                },
            )
        if self.paper_min_mm <= 0 or self.paper_max_mm < self.paper_min_mm:
            raise ValidationError(
                "The tolerance clamp band is not valid: the smallest value must be "
                "above zero and no larger than the biggest.",
                detail={"paper_min_mm": self.paper_min_mm, "paper_max_mm": self.paper_max_mm},
            )
        if self.dpi < 1:
            raise ValidationError(
                "The comparison resolution must be at least 1 DPI.", detail={"dpi": self.dpi}
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_site_mm": self.position_site_mm,
            "min_region_site_mm": self.min_region_site_mm,
            "paper_min_mm": self.paper_min_mm,
            "paper_max_mm": self.paper_max_mm,
            "fallback_position_paper_mm": self.fallback_position_paper_mm,
            "fallback_min_region_paper_mm": self.fallback_min_region_paper_mm,
            "dpi": self.dpi,
        }


@dataclass(frozen=True, slots=True)
class ToleranceValue:
    """One tolerance in all three units, plus whether it was clamped."""

    paper_mm: float
    px: float
    #: None when the sheet scale is unknown — never invent a site figure.
    site_mm: float | None
    #: What the conversion produced before the clamp, in paper mm.
    requested_paper_mm: float
    clamped: bool

    @property
    def px_area(self) -> float:
        """The square of the length, for a minimum-area test on a region."""
        return self.px * self.px


@dataclass(frozen=True, slots=True)
class ResolvedTolerance:
    """The tolerances for one sheet, and how they were arrived at."""

    position: ToleranceValue
    min_region: ToleranceValue
    dpi: int
    px_per_mm: float
    scale_denominator: int | None
    scale_known: bool
    #: What the title block said, for the explanation line.
    scale_text: str | None = None
    #: True when the user overrode the tolerance for this sheet.
    overridden: bool = False
    note: str = ""

    @property
    def clamped(self) -> bool:
        return self.position.clamped or self.min_region.clamped

    def px_to_paper_mm(self, pixels: float) -> float:
        return pixels / self.px_per_mm if self.px_per_mm > 0 else 0.0

    def px_to_site_mm(self, pixels: float) -> float | None:
        """Pixels as millimetres on site, or None without a known scale."""
        if not self.scale_denominator:
            return None
        return self.px_to_paper_mm(pixels) * self.scale_denominator

    def paper_mm_to_px(self, paper_mm: float) -> float:
        return paper_mm * self.px_per_mm

    def site_mm_to_px(self, site_mm: float) -> float | None:
        if not self.scale_denominator:
            return None
        return self.paper_mm_to_px(site_mm / self.scale_denominator)

    def as_dict(self) -> dict[str, Any]:
        def value(item: ToleranceValue) -> dict[str, Any]:
            return {
                "paper_mm": round(item.paper_mm, 4),
                "site_mm": round(item.site_mm, 2) if item.site_mm is not None else None,
                "requested_paper_mm": round(item.requested_paper_mm, 4),
                "clamped": item.clamped,
            }

        return {
            "position": value(self.position),
            "min_region": value(self.min_region),
            "dpi": self.dpi,
            "scale": self.scale_text,
            "scale_denominator": self.scale_denominator,
            "scale_known": self.scale_known,
            "overridden": self.overridden,
            "note": self.note,
        }


@dataclass(slots=True)
class SheetToleranceOverride:
    """A per-sheet override, stored with the comparison run.

    One drawing in a set sometimes needs different treatment — a busy
    coordination sheet, or a detail replotted at a different size. The
    override is recorded so the run stays reproducible.
    """

    position_site_mm: float | None = None
    min_region_site_mm: float | None = None
    #: Forces a scale when the title block could not be read.
    scale_denominator: int | None = None
    reason: str = ""


def _convert(
    site_mm: float,
    *,
    denominator: int | None,
    fallback_paper_mm: float,
    spec: ToleranceSpec,
    px_per_mm: float,
) -> ToleranceValue:
    """One site-millimetre figure resolved to paper millimetres and pixels."""
    # No scale: the site figure is meaningless, so fall back to a paper
    # default and say so rather than pretending the conversion happened.
    requested = site_mm / denominator if denominator else fallback_paper_mm

    paper = min(max(requested, spec.paper_min_mm), spec.paper_max_mm)
    clamped = abs(paper - requested) > 1e-9
    return ToleranceValue(
        paper_mm=paper,
        px=paper * px_per_mm,
        site_mm=paper * denominator if denominator else None,
        requested_paper_mm=requested,
        clamped=clamped,
    )


def scale_denominator_of(scale_text: str | None) -> int | None:
    """The N of 1:N in a title block scale string, or None.

    `NTS`, `As shown` and anything unparsable give None — which is the honest
    answer, and the signal to fall back to the paper default.
    """
    if not scale_text:
        return None
    reading = read_scale(scale_text)
    return reading.ratio


def resolve_for_sheet(
    spec: ToleranceSpec,
    *,
    scale_text: str | None = None,
    scale_denominator: int | None = None,
    dpi: int | None = None,
    override: SheetToleranceOverride | None = None,
) -> ResolvedTolerance:
    """Resolve *spec* for one sheet, using that sheet's own drawing scale.

    ``scale_denominator`` wins over ``scale_text`` when both are given; an
    override wins over both. Returns the values in paper millimetres, site
    millimetres and pixels, with the clamp state on each, so the UI can show
    the effective tolerance for every sheet and nothing is hidden.
    """
    dpi = dpi or spec.dpi
    if dpi < 1:
        raise ValidationError("The comparison resolution must be at least 1 DPI.")

    denominator = scale_denominator or scale_denominator_of(scale_text)
    overridden = False
    position_site = spec.position_site_mm
    region_site = spec.min_region_site_mm

    if override is not None:
        if override.scale_denominator:
            denominator = override.scale_denominator
            overridden = True
        if override.position_site_mm is not None:
            position_site = override.position_site_mm
            overridden = True
        if override.min_region_site_mm is not None:
            region_site = override.min_region_site_mm
            overridden = True

    factor = px_per_mm_at(dpi)
    position = _convert(
        position_site,
        denominator=denominator,
        fallback_paper_mm=spec.fallback_position_paper_mm,
        spec=spec,
        px_per_mm=factor,
    )
    min_region = _convert(
        region_site,
        denominator=denominator,
        fallback_paper_mm=spec.fallback_min_region_paper_mm,
        spec=spec,
        px_per_mm=factor,
    )

    notes: list[str] = []
    if denominator is None:
        notes.append(
            f"This sheet has no drawing scale, so tolerances are set on paper "
            f"({position.paper_mm:.2g} mm)."
        )
    if position.clamped or min_region.clamped:
        notes.append(
            f"At 1:{denominator} the requested tolerance would have been "
            f"{position.requested_paper_mm:.2g} mm on paper, so it was limited to "
            f"{spec.paper_min_mm:g}-{spec.paper_max_mm:g} mm."
        )
    if override is not None and override.reason:
        notes.append(override.reason)

    return ResolvedTolerance(
        position=position,
        min_region=min_region,
        dpi=dpi,
        px_per_mm=factor,
        scale_denominator=denominator,
        scale_known=denominator is not None,
        scale_text=scale_text,
        overridden=overridden,
        note=" ".join(notes),
    )


def format_for_user(value_px: float, resolved: ResolvedTolerance) -> str:
    """A length written the way it must always be written for a user.

    Always both figures, never a pixel count:
    ``"0.4 mm on paper (40 mm on site at 1:100)"``.
    """
    paper = resolved.px_to_paper_mm(value_px)
    site = resolved.px_to_site_mm(value_px)
    if site is None:
        return f"{paper:.2f} mm on paper (no drawing scale on this sheet)"
    return f"{paper:.2f} mm on paper ({site:.0f} mm on site at 1:{resolved.scale_denominator})"


def format_area_for_user(area_px: float, resolved: ResolvedTolerance) -> str:
    """An area written in paper millimetres squared and site square metres."""
    side_paper = resolved.px_to_paper_mm(1.0)
    paper_mm2 = area_px * side_paper * side_paper
    if not resolved.scale_denominator:
        return f"{paper_mm2:.1f} mm² on paper (no drawing scale on this sheet)"
    site_m2 = paper_mm2 * (resolved.scale_denominator**2) / 1_000_000.0
    return f"{paper_mm2:.1f} mm² on paper ({site_m2:.2f} m² on site)"


def area_px_to_site_m2(area_px: float, resolved: ResolvedTolerance) -> float | None:
    """Pixel area as square metres on site, or None without a scale."""
    if not resolved.scale_denominator:
        return None
    side_paper = resolved.px_to_paper_mm(1.0)
    paper_mm2 = area_px * side_paper * side_paper
    return paper_mm2 * (resolved.scale_denominator**2) / 1_000_000.0


@dataclass(slots=True)
class ToleranceSnapshot:
    """What a run stores so its result can be reproduced exactly."""

    spec: ToleranceSpec = field(default_factory=ToleranceSpec)
    resolved: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"spec": self.spec.as_dict(), "resolved": self.resolved}, sort_keys=True)
