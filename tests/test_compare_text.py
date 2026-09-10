"""Stage 6: the text layer decides how much a change matters.

A changed dimension moves a few dozen pixels and can move a wall. Ranking
it by ink area files it as trivial, which is the exact failure this app
exists to prevent, so these tests pin the behaviour down.
"""

from __future__ import annotations

import pytest

from engine.compare.text_diff import TextChange, classify_text, text_change_for
from engine.compare.types import CompareConfig
from engine.core.enums import ChangeType, Severity, TextChangeKind
from engine.extract.text_extractor import PageBox, PageText, TextItem

SIZE = (1000, 800)  # width, height in raster pixels


def item(text: str, fx: float, fy: float) -> TextItem:
    """One text run, positioned as a fraction of the page."""
    return TextItem(
        text=text,
        x=fx * 1000,
        y=fy * 800,
        width=40.0,
        height=12.0,
        font_size=12.0,
        fx=fx,
        fy=fy,
        fh=12.0 / 800,
    )


def page(*items: TextItem) -> PageText:
    return PageText(
        page_index=0,
        box=PageBox(x0=0.0, y0=0.0, x1=1000.0, y1=800.0),
        items=list(items),
    )


# ── Classifying what the text is ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["3200", "3 200", "1,250", "450 mm", "12.5", "2400mm"],
)
def test_numbers_are_dimensions(text: str) -> None:
    assert classify_text("", text) is TextChangeKind.DIMENSION


@pytest.mark.parametrize("text", ["D-04", "C12", "W/21", "A 101"])
def test_short_alphanumerics_are_tags(text: str) -> None:
    assert classify_text("", text) is TextChangeKind.TAG


@pytest.mark.parametrize(
    "text",
    ["REFER TO STRUCTURAL DRAWINGS", "Setting out point", "FFL varies"],
)
def test_prose_is_a_note(text: str) -> None:
    assert classify_text("", text) is TextChangeKind.NOTE


def test_a_deleted_dimension_is_still_a_dimension() -> None:
    """Judged on whichever side has text, so deletions are not demoted."""
    assert classify_text("3200", "") is TextChangeKind.DIMENSION


# ── Reading a region's text ────────────────────────────────────────────


def test_identical_text_is_not_a_change() -> None:
    old = page(item("3200", 0.5, 0.5))
    new = page(item("3200", 0.5, 0.5))
    box = (480, 380, 60, 40)
    assert text_change_for(box, old, new, SIZE, SIZE, pad=10) is None


def test_changed_text_is_reported_both_ways() -> None:
    old = page(item("3200", 0.5, 0.5))
    new = page(item("3400", 0.5, 0.5))
    box = (480, 380, 60, 40)
    change = text_change_for(box, old, new, SIZE, SIZE, pad=10)
    assert change is not None
    assert change.old_text == "3200"
    assert change.new_text == "3400"
    assert change.kind is TextChangeKind.DIMENSION


def test_text_outside_the_region_is_ignored() -> None:
    """A change region reads only what sits in it."""
    old = page(item("3200", 0.1, 0.9))
    new = page(item("3400", 0.1, 0.9))
    far_away = (480, 380, 60, 40)
    assert text_change_for(far_away, old, new, SIZE, SIZE, pad=5) is None


def test_a_sheet_with_no_text_layer_makes_no_claim() -> None:
    """A scanned drawing has no text, so nothing is asserted about its text."""
    assert text_change_for((0, 0, 50, 50), None, None, SIZE, SIZE, pad=5) is None


def test_the_old_box_is_used_for_the_old_sheet() -> None:
    """The old sheet is read where the region *was*, not where it now is."""
    old = page(item("3200", 0.2, 0.5))
    new = page(item("3400", 0.5, 0.5))
    new_box = (480, 380, 60, 40)
    old_box = (180, 380, 60, 40)
    change = text_change_for(new_box, old, new, SIZE, SIZE, pad=10, old_box=old_box)
    assert change is not None
    assert (change.old_text, change.new_text) == ("3200", "3400")


# ── How text drives severity ───────────────────────────────────────────


def _region_with_text(change: TextChange | None):
    """Build one region through the real typer, with a stubbed text lookup."""
    import cv2
    import numpy as np

    from engine.compare.change_typer import build_regions
    from engine.compare.cluster import cluster_regions, sort_boxes
    from engine.compare.raster_diff import diff_masks

    config = CompareConfig(dpi=200)
    old = np.full((600, 800), 255, dtype=np.uint8)
    new = old.copy()
    cv2.rectangle(new, (300, 300), (312, 312), 0, -1)  # a tiny mark
    masks = diff_masks(old, new, None, config)
    boxes = sort_boxes(cluster_regions(masks, config))
    regions = build_regions(boxes, masks, config, 100, lambda _box: change)
    assert regions
    return regions[0]


def test_a_tiny_mark_with_no_text_change_stays_trivial() -> None:
    region = _region_with_text(None)
    assert region.is_cosmetic
    assert region.severity is Severity.TRIVIAL


def test_a_tiny_dimension_change_is_critical_not_trivial() -> None:
    """The whole point: 3200 -> 3400 is small, and it matters most."""
    region = _region_with_text(
        TextChange(kind=TextChangeKind.DIMENSION, old_text="3200", new_text="3400")
    )
    assert not region.is_cosmetic
    assert region.severity is Severity.CRITICAL
    assert region.old_text == "3200"
    assert region.new_text == "3400"
    assert region.text_kind == "dimension"


def test_a_tag_change_is_major() -> None:
    region = _region_with_text(
        TextChange(kind=TextChangeKind.TAG, old_text="D-04", new_text="D-06")
    )
    assert region.severity is Severity.MAJOR


def test_the_explanation_quotes_both_versions() -> None:
    region = _region_with_text(
        TextChange(kind=TextChangeKind.DIMENSION, old_text="3200", new_text="3400")
    )
    assert "3200" in region.explanation
    assert "3400" in region.explanation
    assert "px" not in region.explanation.lower()


def test_added_text_reads_as_added() -> None:
    region = _region_with_text(
        TextChange(kind=TextChangeKind.NOTE, old_text="", new_text="SETTING OUT POINT")
    )
    assert "added" in region.explanation.lower()
    assert region.change_type is ChangeType.ADDED
