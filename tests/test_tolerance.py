"""Phase 5 tolerances: the conversion chain and the clamp that saves it.

    site mm  ->  / scale denominator  ->  paper mm  ->  x px_per_mm  ->  px

Two failures this file exists to prevent, both silent: a 1:5 detail whose
tolerance swallows every change, and a 1:500 site plan whose tolerance is
smaller than a pixel so nothing ever matches.
"""

from __future__ import annotations

import pytest

from engine.compare.tolerance import (
    SheetToleranceOverride,
    ToleranceSpec,
    area_px_to_site_m2,
    format_area_for_user,
    format_for_user,
    resolve_for_sheet,
    scale_denominator_of,
)
from engine.utils.errors import ValidationError


def test_the_plans_worked_example():
    """50 mm on site at 1:100 and 200 DPI is 0.5 mm on paper and 3.9 px."""
    spec = ToleranceSpec(position_site_mm=50.0, dpi=200)
    resolved = resolve_for_sheet(spec, scale_text="1:100")

    assert resolved.position.paper_mm == pytest.approx(0.5)
    assert resolved.position.px == pytest.approx(3.937, abs=0.01)
    assert resolved.position.site_mm == pytest.approx(50.0)
    assert not resolved.position.clamped


def test_a_detail_sheet_is_clamped_rather_than_hiding_everything():
    """25 mm on site at 1:5 is 5 mm on paper, which would hide the drawing."""
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:5")

    assert resolved.position.requested_paper_mm == pytest.approx(5.0)
    assert resolved.position.paper_mm == pytest.approx(3.0)  # the clamp
    assert resolved.position.clamped
    assert "limited to" in resolved.note


def test_a_site_plan_is_clamped_rather_than_being_sub_pixel():
    """25 mm on site at 1:500 is 0.05 mm on paper: smaller than one pixel."""
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:500")

    assert resolved.position.requested_paper_mm == pytest.approx(0.05)
    assert resolved.position.paper_mm == pytest.approx(0.2)
    assert resolved.position.clamped


def test_no_scale_falls_back_to_paper_and_says_so():
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="NTS")

    assert not resolved.scale_known
    assert resolved.scale_denominator is None
    assert resolved.position.paper_mm == pytest.approx(0.5)
    assert resolved.position.site_mm is None  # never invented
    assert "no drawing scale" in resolved.note


def test_unreadable_scale_is_the_same_as_none():
    assert scale_denominator_of("Refer to drawing") is None
    assert scale_denominator_of(None) is None
    assert scale_denominator_of("1 : 250") == 250


def test_an_override_wins_and_is_recorded():
    override = SheetToleranceOverride(
        position_site_mm=100.0, scale_denominator=50, reason="Busy coordination sheet."
    )
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:100", override=override)

    assert resolved.overridden
    assert resolved.scale_denominator == 50
    assert resolved.position.paper_mm == pytest.approx(2.0)
    assert "Busy coordination sheet." in resolved.note


def test_a_user_never_sees_a_pixel_count():
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:100", dpi=200)
    text = format_for_user(resolved.position.px, resolved)

    assert "mm on paper" in text
    assert "mm on site" in text
    assert "1:100" in text
    assert "px" not in text
    assert "pixel" not in text


def test_without_a_scale_the_site_figure_is_not_invented():
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="NTS")
    text = format_for_user(resolved.position.px, resolved)

    assert "no drawing scale" in text
    assert "on site" not in text


def test_areas_convert_to_square_metres_on_site():
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:100", dpi=200)
    # A square 100 px on a side at 200 DPI is 12.7 mm on paper, so 1.27 m
    # on site at 1:100 — an area of about 1.6 m2.
    area = area_px_to_site_m2(100 * 100, resolved)

    assert area == pytest.approx(1.613, abs=0.01)
    assert "m²" in format_area_for_user(100 * 100, resolved)


def test_round_trip_between_the_units():
    resolved = resolve_for_sheet(ToleranceSpec(), scale_text="1:200", dpi=300)

    assert resolved.px_to_paper_mm(resolved.paper_mm_to_px(1.234)) == pytest.approx(1.234)
    assert resolved.site_mm_to_px(400.0) == pytest.approx(resolved.paper_mm_to_px(2.0))
    assert resolved.px_to_site_mm(resolved.site_mm_to_px(400.0)) == pytest.approx(400.0)


def test_a_nonsense_specification_is_refused_rather_than_guessed():
    with pytest.raises(ValidationError):
        ToleranceSpec(position_site_mm=0.0)
    with pytest.raises(ValidationError):
        ToleranceSpec(paper_min_mm=3.0, paper_max_mm=0.2)
    with pytest.raises(ValidationError):
        ToleranceSpec(dpi=0)


def test_the_comparison_dpi_is_not_hard_coded():
    """Doubling the resolution doubles the pixels and changes nothing else."""
    spec = ToleranceSpec()
    low = resolve_for_sheet(spec, scale_text="1:100", dpi=200)
    high = resolve_for_sheet(spec, scale_text="1:100", dpi=400)

    assert high.position.paper_mm == pytest.approx(low.position.paper_mm)
    assert high.position.px == pytest.approx(low.position.px * 2)
