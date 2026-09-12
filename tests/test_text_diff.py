"""The text stream: matching, moving, modifying and renumbering.

The highest-value output of the phase, and the one place where a greedy
nearest-match loop would quietly turn one real change into two false ones.
"""

from __future__ import annotations

import pytest

from engine.compare.dimension_diff import (
    analyse_numeric,
    cross_check_geometry,
    estimate_quantity_impact,
    find_stale_dimensions,
)
from engine.compare.text_classify import classify
from engine.compare.text_diff import (
    TextDiffConfig,
    TextItemView,
    detect_renumbering,
    diff_text,
    normalise_text,
)
from engine.compare.tolerance import ToleranceSpec, resolve_for_sheet
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    CrossCheckFlag,
    Stream,
    TextCategory,
    TextChangeDetail,
    VectorChangeDetail,
)

TOLERANCE = resolve_for_sheet(ToleranceSpec(dpi=200), scale_text="1:100", dpi=200)


def item(text: str, x: float, y: float, width: float = 40.0, height: float = 12.0) -> TextItemView:
    box = Bbox(x, y, width, height)
    result = classify(text, box)
    return TextItemView(
        text=text,
        normalised=normalise_text(text),
        bbox=box,
        category=result.category,
        category_confidence=result.confidence,
        number=result.number,
    )


def kinds(changes: list[ChangeRecord]) -> list[ChangeKind]:
    return [change.kind for change in changes]


# ── The gate ────────────────────────────────────────────────────────────


def test_identical_text_reports_nothing():
    items = [item("3000", 100, 100), item("D-12", 300, 100), item("BEDROOM 1", 500, 200)]
    result = diff_text(items, [item(i.text, i.bbox.x, i.bbox.y) for i in items], TOLERANCE)

    assert result.changes == []
    assert result.unchanged_count == 3


# ── Matching ────────────────────────────────────────────────────────────


def test_a_changed_dimension_is_one_modification_with_its_delta():
    result = diff_text([item("3000", 100, 100)], [item("3200", 100, 100)], TOLERANCE)

    assert kinds(result.changes) == [ChangeKind.MODIFIED]
    change = result.changes[0]
    assert change.text is not None
    assert change.text.old_text == "3000"
    assert change.text.new_text == "3200"
    assert change.text.numeric_delta == pytest.approx(200.0)
    assert change.text.percent_delta == pytest.approx(6.667, abs=0.01)
    assert "+200 mm" in change.description
    assert "6.7%" in change.description


def test_text_that_moved_is_moved_not_removed_and_added():
    result = diff_text([item("D-12", 100, 100)], [item("D-12", 900, 700)], TOLERANCE)

    assert kinds(result.changes) == [ChangeKind.MOVED]


def test_text_that_shifted_within_its_own_box_has_not_moved():
    """A substituted font moves every centre without moving any text."""
    result = diff_text([item("BEDROOM 1", 100, 100)], [item("BEDROOM 1", 104, 100)], TOLERANCE)

    assert result.changes == []
    assert result.unchanged_count == 1


def test_added_and_removed_text_when_nothing_is_close():
    result = diff_text([item("OLD NOTE HERE", 100, 100)], [item("W3", 3000, 2000)], TOLERANCE)

    assert set(kinds(result.changes)) == {ChangeKind.ADDED, ChangeKind.REMOVED}


def test_optimal_assignment_pairs_a_dimension_string_correctly():
    """Five numbers within centimetres of each other, one of them changed.

    A greedy nearest-match loop pairs the changed value with its neighbour
    and reports two changes where there is one. This is the case the plan
    calls out, and the reason assignment is solved rather than looped.
    """
    old = [
        item(text, 100 + index * 60, 100)
        for index, text in enumerate(["1200", "1800", "2400", "3000", "3600"])
    ]
    new = [
        item(text, 100 + index * 60, 100)
        for index, text in enumerate(["1200", "1800", "2450", "3000", "3600"])
    ]

    result = diff_text(old, new, TOLERANCE)

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.text is not None
    assert (change.text.old_text, change.text.new_text) == ("2400", "2450")


def test_repeated_labels_pair_with_their_nearest_copy():
    """Twelve doors all labelled D-12 must not report as twelve moves."""
    old = [item("D-12", 100 + index * 200, 100) for index in range(12)]
    new = [item("D-12", 100 + index * 200, 100) for index in range(12)]

    result = diff_text(old, new, TOLERANCE)

    assert result.changes == []
    assert result.unchanged_count == 12


# ── Renumbering ─────────────────────────────────────────────────────────


def test_bulk_renumbering_is_one_record_with_the_mapping():
    old = [item(f"D-{n}", 100 + n * 100, 100) for n in (12, 13, 14, 15)]
    new = [item(f"D-{n + 10}", 100 + n * 100, 100) for n in (12, 13, 14, 15)]

    result = diff_text(old, new, TOLERANCE)

    assert kinds(result.changes) == [ChangeKind.TAGS_RENUMBERED]
    assert result.renumbering is not None
    assert result.renumbering.mapping["D-12"] == "D-22"
    assert "+10" in result.renumbering.rule
    assert "4 tags renumbered" in result.changes[0].description


def test_a_few_unrelated_tag_edits_stay_separate():
    """Three inconsistent edits are three findings a user wants to see."""
    old = [item("D-12", 100, 100), item("W3", 300, 100), item("C1", 500, 100)]
    new = [item("D-19", 100, 100), item("W7", 300, 100), item("C4", 500, 100)]

    result = diff_text(old, new, TOLERANCE)

    assert result.renumbering is None
    assert len(result.changes) == 3


def test_a_mapping_that_is_not_a_bijection_is_not_a_renumbering():
    changes = [
        ChangeRecord(
            kind=ChangeKind.MODIFIED,
            bbox=Bbox(0, 0, 10, 10),
            streams=[Stream.TEXT],
            text=TextChangeDetail(category=TextCategory.TAG, old_text=f"D-{n}", new_text="D-99"),
        )
        for n in (12, 13, 14, 15)
    ]
    assert detect_renumbering(changes, TextDiffConfig()) is None


# ── Numeric analysis ────────────────────────────────────────────────────


def test_a_level_is_reported_in_millimetres_not_metres():
    analysis = analyse_numeric("+3.600", "+3.750", category=TextCategory.LEVEL)
    assert analysis.delta == pytest.approx(150.0)
    assert analysis.direction == "raised"
    assert "+150 mm" in analysis.description


def test_non_numeric_text_is_described_plainly_not_forced_into_numbers():
    analysis = analyse_numeric("1 HOUR FIRE RATED", "2 HOUR FIRE RATED")
    assert analysis.delta is None
    assert analysis.direction == "unknown"
    assert "1 HOUR FIRE RATED" in analysis.description


# ── Cross-checking against geometry ─────────────────────────────────────


def _dimension_change() -> ChangeRecord:
    return ChangeRecord(
        kind=ChangeKind.MODIFIED,
        bbox=Bbox(100, 100, 40, 12),
        streams=[Stream.TEXT],
        text=TextChangeDetail(
            category=TextCategory.DIMENSION,
            old_text="3000",
            new_text="3200",
            numeric_delta=200.0,
        ),
    )


def test_a_dimension_that_changed_with_nothing_moving_is_flagged():
    result = cross_check_geometry(_dimension_change(), [], radius_px=50)

    assert result.flag is CrossCheckFlag.DIMENSION_TEXT_ONLY
    assert "not to scale" in result.message


def test_a_dimension_that_changed_with_geometry_moving_is_consistent():
    geometry = ChangeRecord(
        kind=ChangeKind.MOVED, bbox=Bbox(110, 110, 60, 60), streams=[Stream.VECTOR]
    )
    result = cross_check_geometry(_dimension_change(), [geometry], radius_px=50)

    assert result.flag is CrossCheckFlag.CONSISTENT
    assert result.nearby_geometry_changes == 1


def test_geometry_that_moved_beside_an_unchanged_dimension_is_raised():
    geometry = [
        ChangeRecord(kind=ChangeKind.MOVED, bbox=Bbox(110, 110, 60, 60), streams=[Stream.VECTOR])
    ]
    findings = find_stale_dimensions(geometry, [(Bbox(100, 100, 40, 12), "3000")], 50)

    assert len(findings) == 1
    _box, text, check = findings[0]
    assert text == "3000"
    assert check.flag is CrossCheckFlag.GEOMETRY_MOVED_DIMENSION_STALE


# ── Never guess a quantity ──────────────────────────────────────────────


def test_a_quantity_is_produced_only_when_one_element_moved():
    change = _dimension_change()
    line = ChangeRecord(
        kind=ChangeKind.MOVED,
        bbox=Bbox(110, 110, 60, 60),
        streams=[Stream.VECTOR],
        vector=VectorChangeDetail(geometry_type="line"),
    )
    impact = estimate_quantity_impact(change, [line], 50)

    assert impact is not None
    assert impact.length_delta_mm == pytest.approx(200.0)


def test_two_candidate_elements_produce_no_quantity_at_all():
    change = _dimension_change()
    lines = [
        ChangeRecord(
            kind=ChangeKind.MOVED,
            bbox=Bbox(110, 110, 60, 60),
            streams=[Stream.VECTOR],
            vector=VectorChangeDetail(geometry_type="line"),
        ),
        ChangeRecord(
            kind=ChangeKind.MOVED,
            bbox=Bbox(120, 120, 60, 60),
            streams=[Stream.VECTOR],
            vector=VectorChangeDetail(geometry_type="line"),
        ),
    ]
    assert estimate_quantity_impact(change, lines, 50) is None


def test_no_geometry_at_all_produces_no_quantity():
    assert estimate_quantity_impact(_dimension_change(), [], 50) is None
