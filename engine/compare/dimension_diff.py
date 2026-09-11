"""Task 5.7 — numeric and dimension analysis.

A change that reads `3000 -> 3200` is information. A change that reads
`3000 -> 3200 (+200 mm, +6.7%)` is a finding a quantity surveyor can act on
without going back to the drawing. That is the whole job of this module.

It also handles the two traps from the plan, which are what make a QS trust
the tool rather than merely read it:

* **The dimension text changed but nothing near it moved.** Either the
  drawing was wrong before, or the item is marked not to scale. Real finding.
* **Geometry moved but the dimension beside it still reads the old value.**
  Usually a drafting error, and always worth raising.

One hard rule, from the top of the project: **never guess a quantity.**
:func:`estimate_quantity_impact` returns ``None`` whenever the geometry is
not unambiguous, and the caller reports the change without a quantity rather
than reporting a number nobody can stand behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.compare.text_classify import ParsedNumber, parse_numeric
from engine.compare.types import Bbox, ChangeRecord, CrossCheckFlag, TextCategory

#: Levels are written in metres; dimensions in millimetres. Mixing the two is
#: the classic unit bug on a drawing, so the conversion is explicit.
METRES_TO_MM = 1000.0

#: A percentage change smaller than this is reported as "no material change"
#: rather than as a percentage, because 0.01% is noise in a text reading.
NEGLIGIBLE_PERCENT = 0.05


@dataclass(frozen=True, slots=True)
class NumericAnalysis:
    """What changed, numerically, in the units the drawing uses."""

    old_value: float | None
    new_value: float | None
    #: New minus old, in the drawing's own units.
    delta: float | None
    percent_delta: float | None
    #: The delta in real-world millimetres at drawing scale, when known.
    site_delta_mm: float | None
    unit: str
    direction: str  # "increased" | "decreased" | "unchanged" | "unknown"
    description: str

    @property
    def is_numeric(self) -> bool:
        return self.old_value is not None and self.new_value is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": self.delta,
            "percent_delta": self.percent_delta,
            "site_delta_mm": self.site_delta_mm,
            "unit": self.unit,
            "direction": self.direction,
            "description": self.description,
        }


def _format_number(value: float) -> str:
    """Drawings write whole millimetres without a decimal point."""
    if abs(value - round(value)) < 1e-6:
        return f"{round(value)}"
    return f"{value:g}"


def analyse_numeric(
    old_text: str | None,
    new_text: str | None,
    *,
    category: TextCategory = TextCategory.DIMENSION,
    scale_denominator: int | None = None,
    old_number: ParsedNumber | None = None,
    new_number: ParsedNumber | None = None,
) -> NumericAnalysis:
    """Compare two pieces of text as numbers, when they parse as numbers.

    A text change where either side is not a number is not a failure: the
    result simply carries ``direction="unknown"`` and a plain description,
    which is the honest output for `1 HOUR FIRE RATED` becoming `2 HOUR`.

    ``old_number``/``new_number`` let the caller pass the value the
    classifier already extracted. That matters for prefixed text: `FFL +3.600`
    does not parse as a number on its own, but the classifier read `+3.600`
    out of it and throwing that away would lose the 150 mm delta.
    """
    old_number = old_number or parse_numeric(old_text or "")
    new_number = new_number or parse_numeric(new_text or "")

    if old_number is None or new_number is None:
        return NumericAnalysis(
            old_value=old_number.value if old_number else None,
            new_value=new_number.value if new_number else None,
            delta=None,
            percent_delta=None,
            site_delta_mm=None,
            unit="",
            direction="unknown",
            description=f"{old_text or '(nothing)'} → {new_text or '(nothing)'}",
        )

    if category is TextCategory.LEVEL:
        return _analyse_level(old_number, new_number, old_text, new_text)

    old_mm = old_number.in_millimetres
    new_mm = new_number.in_millimetres
    delta = new_mm - old_mm
    percent = (delta / old_mm * 100.0) if old_mm else None
    unit = (new_number.unit or old_number.unit or "mm").lower()

    # A dimension on a drawing is already a site dimension: the number is
    # what will be built. The scale only matters for lengths measured off the
    # sheet, which is a different question and handled in the raster stream.
    site_delta = delta

    direction = "unchanged"
    if delta > 0:
        direction = "increased"
    elif delta < 0:
        direction = "decreased"

    parts = [f"{_format_number(old_number.value)} → {_format_number(new_number.value)}"]
    if abs(delta) > 0:
        detail = f"{delta:+.0f} mm" if abs(delta) >= 1 else f"{delta:+.2f} mm"
        if percent is not None and abs(percent) >= NEGLIGIBLE_PERCENT:
            detail += f", {percent:+.1f}%"
        parts.append(f"({detail})")

    return NumericAnalysis(
        old_value=old_number.value,
        new_value=new_number.value,
        delta=delta,
        percent_delta=percent,
        site_delta_mm=site_delta,
        unit=unit,
        direction=direction,
        description=" ".join(parts),
    )


def _analyse_level(
    old_number: ParsedNumber,
    new_number: ParsedNumber,
    old_text: str | None,
    new_text: str | None,
) -> NumericAnalysis:
    """Levels are metres where dimensions are millimetres.

    `+3.600` to `+3.750` is 150 mm of slab or finish, not 0.15 of anything.
    Reporting it in millimetres is what makes it comparable with every other
    change on the sheet.
    """
    old_mm = old_number.value * METRES_TO_MM
    new_mm = new_number.value * METRES_TO_MM
    delta_mm = new_mm - old_mm
    percent = (delta_mm / old_mm * 100.0) if old_mm else None

    direction = "unchanged"
    if delta_mm > 0:
        direction = "raised"
    elif delta_mm < 0:
        direction = "lowered"

    description = f"{old_text or old_number.raw} → {new_text or new_number.raw}"
    if abs(delta_mm) > 0:
        description += f" ({delta_mm:+.0f} mm)"

    return NumericAnalysis(
        old_value=old_number.value,
        new_value=new_number.value,
        delta=delta_mm,
        percent_delta=percent,
        site_delta_mm=delta_mm,
        unit="m",
        direction=direction,
        description=description,
    )


def analyse_level_change(old_text: str | None, new_text: str | None) -> NumericAnalysis:
    """A level change, always reported in millimetres."""
    return analyse_numeric(old_text, new_text, category=TextCategory.LEVEL)


def analyse_numeric_change(
    change: ChangeRecord, scale_denominator: int | None = None
) -> NumericAnalysis:
    """Analyse a change record in place, filling in its text detail."""
    detail = change.text
    if detail is None:
        return NumericAnalysis(None, None, None, None, None, "", "unknown", change.description)

    analysis = analyse_numeric(
        detail.old_text,
        detail.new_text,
        category=detail.category,
        scale_denominator=scale_denominator,
    )
    detail.numeric_delta = analysis.delta
    detail.percent_delta = analysis.percent_delta
    detail.site_delta_mm = analysis.site_delta_mm
    if analysis.is_numeric:
        label = "Level" if detail.category is TextCategory.LEVEL else "Dimension"
        if detail.category not in {TextCategory.DIMENSION, TextCategory.LEVEL}:
            label = str(detail.category).title()
        change.description = f"{label} changed: {analysis.description}"
    return analysis


# ── Cross-checking text against geometry ────────────────────────────────


@dataclass(frozen=True, slots=True)
class CrossCheckResult:
    """Whether a dimension and the geometry beside it tell the same story."""

    flag: CrossCheckFlag
    message: str
    #: How many geometry changes were found within the search radius.
    nearby_geometry_changes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "flag": str(self.flag),
            "message": self.message,
            "nearby_geometry_changes": self.nearby_geometry_changes,
        }


def cross_check_geometry(
    text_change: ChangeRecord,
    geometry_changes: list[ChangeRecord],
    radius_px: float,
) -> CrossCheckResult:
    """Does the geometry near a changed dimension agree with it?

    Both disagreements are real findings and both are reported explicitly.
    Saying "the dimension changed but nothing moved" out loud is the kind of
    observation that makes a quantity surveyor trust the rest of the report.
    """
    detail = text_change.text
    if detail is None or detail.category not in {TextCategory.DIMENSION, TextCategory.LEVEL}:
        return CrossCheckResult(CrossCheckFlag.UNKNOWN, "Not a dimension, so nothing to check.")

    search = text_change.bbox.expanded(radius_px)
    nearby = [change for change in geometry_changes if search.intersects(change.bbox)]

    if nearby:
        return CrossCheckResult(
            CrossCheckFlag.CONSISTENT,
            f"The drawing moved here too ({len(nearby)} nearby geometry changes).",
            len(nearby),
        )
    return CrossCheckResult(
        CrossCheckFlag.DIMENSION_TEXT_ONLY,
        "The dimension text changed but nothing moved near it. Either the drawing "
        "was wrong before, or this item is not to scale.",
        0,
    )


def find_stale_dimensions(
    geometry_changes: list[ChangeRecord],
    unchanged_dimension_boxes: list[tuple[Bbox, str]],
    radius_px: float,
) -> list[tuple[Bbox, str, CrossCheckResult]]:
    """The other direction: geometry moved, the dimension still reads the old value.

    ``unchanged_dimension_boxes`` is every dimension the text stream reported
    as unchanged, with its text. One near a geometry change is a drafting
    error worth raising — the tool is telling the user something the designer
    has not noticed yet.
    """
    findings: list[tuple[Bbox, str, CrossCheckResult]] = []
    for box, text in unchanged_dimension_boxes:
        search = box.expanded(radius_px)
        nearby = [change for change in geometry_changes if search.intersects(change.bbox)]
        if not nearby:
            continue
        findings.append(
            (
                box,
                text,
                CrossCheckResult(
                    CrossCheckFlag.GEOMETRY_MOVED_DIMENSION_STALE,
                    f"The drawing moved here but the dimension still reads {text}. "
                    "Check whether the dimension was updated.",
                    len(nearby),
                ),
            )
        )
    return findings


# ── Quantity impact ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QuantityImpact:
    """A length delta on a linear element. Only ever produced when certain."""

    length_delta_mm: float
    element: str
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "length_delta_mm": self.length_delta_mm,
            "element": self.element,
            "basis": self.basis,
        }


def estimate_quantity_impact(
    change: ChangeRecord,
    geometry_changes: list[ChangeRecord],
    radius_px: float,
) -> QuantityImpact | None:
    """The length delta implied by a dimension change, or None.

    Returns None whenever anything is ambiguous — no numeric delta, no
    geometry to attribute it to, or more than one candidate element nearby.
    **Never guess a quantity.** A change reported without a quantity is
    useful; a change reported with a wrong one is worse than useless.
    """
    detail = change.text
    if detail is None or detail.numeric_delta is None or detail.numeric_delta == 0:
        return None
    if detail.category is not TextCategory.DIMENSION:
        return None

    search = change.bbox.expanded(radius_px)
    nearby = [item for item in geometry_changes if search.intersects(item.bbox)]
    if len(nearby) != 1:
        # Zero: nothing to attribute the change to. More than one: we cannot
        # say which element the dimension governs.
        return None

    element = nearby[0]
    geometry_type = element.vector.geometry_type if element.vector else "region"
    if geometry_type not in {"line", "polyline", "rectangle"}:
        return None

    return QuantityImpact(
        length_delta_mm=float(detail.numeric_delta),
        element=geometry_type,
        basis=(
            "One dimension changed and exactly one linear element moved beside it, "
            "so the length delta is the dimension delta."
        ),
    )
