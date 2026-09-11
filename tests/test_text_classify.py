"""Classifying drawing text, and reading the numbers out of it.

The categories drive how a change is described and how much it is likely to
cost, so a note filed as a dimension gets a numeric analysis it has no
business having. `unknown` is a real answer and better than a wrong one.
"""

from __future__ import annotations

import pytest

from engine.compare.text_classify import (
    ClassifyContext,
    classify,
    parse_numeric,
)
from engine.compare.types import Bbox, TextCategory


def category(text: str, context: ClassifyContext | None = None) -> TextCategory:
    return classify(text, Bbox(0, 0, 10, 5), context).category


# ── Numbers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("3000", 3000.0, ""),
        ("3,000", 3000.0, ""),
        ("2400mm", 2400.0, "MM"),
        ("2400 mm", 2400.0, "MM"),
        ("2.4m", 2.4, "M"),
        ("+3.600", 3.6, ""),
        ("-0.150", -0.15, ""),
    ],
)
def test_numbers_are_read_the_way_drawings_write_them(text, value, unit):
    parsed = parse_numeric(text)
    assert parsed is not None
    assert parsed.value == pytest.approx(value)
    assert parsed.unit.upper() == unit


def test_a_dimension_string_leads_with_its_first_value():
    parsed = parse_numeric("3000/2400/1800")
    assert parsed is not None
    assert parsed.kind == "string"
    assert parsed.value == pytest.approx(3000.0)
    assert parsed.values == (3000.0, 2400.0, 1800.0)


def test_a_range_keeps_both_ends():
    parsed = parse_numeric("3000-3200")
    assert parsed is not None
    assert parsed.kind == "range"
    assert parsed.values == (3000.0, 3200.0)


def test_units_convert_to_millimetres():
    assert parse_numeric("2.4m").in_millimetres == pytest.approx(2400.0)
    assert parse_numeric("2400mm").in_millimetres == pytest.approx(2400.0)
    assert parse_numeric("3000").in_millimetres == pytest.approx(3000.0)


def test_text_that_is_not_a_number_returns_none():
    assert parse_numeric("FIRE RATED") is None
    assert parse_numeric("") is None
    assert parse_numeric("D-12") is None


# ── Categories ──────────────────────────────────────────────────────────


def test_a_scale_is_not_a_dimension():
    """`1:100` parses as text containing numbers; it is a scale first."""
    assert category("1:100") is TextCategory.SCALE
    assert category("1 : 50") is TextCategory.SCALE
    assert category("NTS") is TextCategory.SCALE
    assert category("AS SHOWN") is TextCategory.SCALE


@pytest.mark.parametrize("text", ["+3.600", "-0.150", "FFL +0.150", "SSL 24.250", "IL 12.340"])
def test_levels_are_recognised_with_and_without_a_prefix(text):
    assert category(text) is TextCategory.LEVEL


@pytest.mark.parametrize("text", ["D-12", "W3", "C1", "FD-60", "P12"])
def test_tags_are_short_and_alphanumeric(text):
    assert category(text) is TextCategory.TAG


def test_a_number_alone_is_a_dimension_but_less_confidently():
    result = classify("3000", Bbox(0, 0, 20, 8))
    assert result.category is TextCategory.DIMENSION
    assert result.confidence < 0.9
    assert "no dimension line" in result.evidence


def test_a_number_on_a_dimension_line_is_certain():
    context = ClassifyContext(dimension_lines=[Bbox(0, 20, 200, 2)], px_per_mm=200 / 25.4)
    result = classify("3000", Bbox(80, 5, 20, 8), context)

    assert result.category is TextCategory.DIMENSION
    assert result.confidence >= 0.9


def test_a_letter_in_a_bubble_is_a_grid_reference():
    context = ClassifyContext(enclosures=[Bbox(0, 0, 40, 40)])
    assert category("A", context) is TextCategory.GRID
    assert category("12", context) is TextCategory.GRID


def test_prose_is_a_note():
    assert category("Provide 100mm blockwork throughout to all party walls") is TextCategory.NOTE


def test_specification_wording_outranks_prose():
    assert category("2 HOUR FIRE RATED PARTITION THROUGHOUT") is TextCategory.SPEC
    assert category("1 HOUR FIRE RATED") is TextCategory.SPEC


def test_text_inside_a_closed_region_is_a_room():
    context = ClassifyContext(room_regions=[Bbox(0, 0, 400, 400)])
    result = classify("BEDROOM 1", Bbox(100, 100, 80, 12), context)
    assert result.category is TextCategory.ROOM


def test_masked_text_is_title_block_before_anything_else():
    result = classify("1:100", Bbox(0, 0, 10, 5), ClassifyContext(masked=True))
    assert result.category is TextCategory.TITLEBLOCK
    assert result.confidence == 1.0


def test_nothing_matching_is_an_honest_unknown():
    result = classify("@@@", Bbox(0, 0, 10, 5))
    assert result.category is TextCategory.UNKNOWN
    assert result.confidence < 0.5


def test_every_classification_carries_its_evidence():
    for text in ("3000", "1:100", "FFL +3.600", "D-12", "1 HOUR FIRE RATED"):
        result = classify(text, Bbox(0, 0, 10, 5))
        assert result.evidence, f"{text} was classified with no reason given"
