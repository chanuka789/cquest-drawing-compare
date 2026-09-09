"""Anchor extraction and matching tests: Task 4.4.

The pages here are synthetic by hand — plain :class:`PageText` objects built
from :class:`TextItem` runs in PDF user space — so no PDF IO is needed and
every property can be asserted exactly: which strings survive the title
block exclusion, whether repeats are dropped, and where the anchor pixels
land for a media box whose origin is NOT at (0, 0) (the Lami fixture box,
(-1192, -842) to (1192, 842)).
"""

from __future__ import annotations

import pytest

from engine.align.anchors import (
    assess_anchor_quality,
    extract_text_anchors,
    find_correspondences,
    normalise_anchor_text,
    page_points_to_image_px,
)
from engine.align.types import Anchor, Correspondence
from engine.extract.text_extractor import PageBox, PageText, TextItem

DPI = 200

#: Offset-origin media box, as seen on the Lami Architects fixture.
OFFSET_BOX = PageBox(x0=-1192.0, y0=-842.0, x1=1192.0, y1=842.0)
#: The same sheet geometry with the box at the origin.
ORIGIN_BOX = PageBox(x0=0.0, y0=0.0, x1=2384.0, y1=1684.0)

#: Title block cluster: bottom-right, full of title-block vocabulary.
TITLE_ITEMS: list[tuple[str, float, float, float]] = [
    ("PROJECT: ACME TOWER", 0.80, 0.24, 240.0),
    ("SCALE 1:100", 0.80, 0.14, 180.0),
    ("REV C", 0.93, 0.05, 90.0),
    ("DRAWN J. SMITH", 0.80, 0.02, 220.0),
]

#: Body content of the old sheet: unique labels, plus traps — repeats of the
#: same string, pure punctuation, single characters, a symbol-only string.
OLD_CONTENT: list[tuple[str, float, float, float]] = [
    ("RM-01", 0.10, 0.80, 120.0),
    ("RM-02", 0.12, 0.60, 120.0),
    ("RM-03 3000", 0.36, 0.76, 220.0),
    ("RM-04", 0.52, 0.45, 120.0),
    ("RM-05", 0.30, 0.34, 120.0),
    ("DOOR D1", 0.58, 0.62, 160.0),
    ("TYP", 0.45, 0.88, 70.0),
    ("TYP", 0.55, 0.90, 70.0),
    ("1500", 0.20, 0.44, 90.0),
    ("1500", 0.42, 0.52, 90.0),
    ("----------", 0.60, 0.70, 180.0),
    ("....", 0.25, 0.28, 80.0),
    ("A", 0.62, 0.86, 40.0),
    ("1", 0.18, 0.88, 40.0),
    ("+/-", 0.55, 0.30, 60.0),
]

HEIGHT_PT = 18.0
TITLE_HEIGHT_PT = 16.0


def _text_item(
    box: PageBox,
    text: str,
    fx: float,
    fy: float,
    width_pt: float,
    height_pt: float,
    x_shift_pt: float = 0.0,
) -> TextItem:
    x_pt = box.x0 + fx * box.width + x_shift_pt
    y_pt = box.y0 + fy * box.height
    return TextItem(
        text=text,
        x=x_pt,
        y=y_pt,
        width=width_pt,
        height=height_pt,
        font_size=round(height_pt * 0.85, 2),
        fx=box.fraction_x(x_pt),
        fy=box.fraction_y(y_pt),
        fh=height_pt / box.height,
    )


def _make_page(
    box: PageBox,
    content: list[tuple[str, float, float, float]],
    x_shift_pt: float = 0.0,
    include_title: bool = True,
) -> PageText:
    items: list[TextItem] = []
    if include_title:
        for text, fx, fy, width in TITLE_ITEMS:
            items.append(_text_item(box, text, fx, fy, width, TITLE_HEIGHT_PT))
    for text, fx, fy, width in content:
        items.append(_text_item(box, text, fx, fy, width, HEIGHT_PT, x_shift_pt))
    return PageText(page_index=0, box=box, items=items)


def _content_labels(
    box: PageBox, content: list[tuple[str, float, float, float]]
) -> dict[str, Anchor]:
    """Extract anchors and index them by normalised text for assertions."""
    return {anchor.text: anchor for anchor in extract_text_anchors(_make_page(box, content), DPI)}


# ── Coordinate conversion ────────────────────────────────────────────────


def test_page_points_to_image_px_flips_y_and_ignores_box_origin():
    # A point at the box origin maps to the bottom-left pixel, not (0, 0).
    x, y = page_points_to_image_px(
        OFFSET_BOX.x0,
        OFFSET_BOX.y0,
        OFFSET_BOX.x0,
        OFFSET_BOX.y0,
        OFFSET_BOX.width,
        OFFSET_BOX.height,
        DPI,
    )
    assert (x, y) == pytest.approx((0.0, OFFSET_BOX.height * DPI / 72.0))
    # The top-left corner is pixel y = 0: PDF y-up becomes image y-down.
    x, y = page_points_to_image_px(
        OFFSET_BOX.x0,
        OFFSET_BOX.y1,
        OFFSET_BOX.x0,
        OFFSET_BOX.y0,
        OFFSET_BOX.width,
        OFFSET_BOX.height,
        DPI,
    )
    assert (x, y) == pytest.approx((0.0, 0.0))
    # Identical relative geometry gives identical pixels regardless of where
    # the media box sits.
    relative = (300.0, 400.0)
    offset = page_points_to_image_px(
        OFFSET_BOX.x0 + relative[0],
        OFFSET_BOX.y0 + relative[1],
        OFFSET_BOX.x0,
        OFFSET_BOX.y0,
        OFFSET_BOX.width,
        OFFSET_BOX.height,
        DPI,
    )
    origin = page_points_to_image_px(
        relative[0],
        relative[1],
        0.0,
        0.0,
        OFFSET_BOX.width,
        OFFSET_BOX.height,
        DPI,
    )
    assert offset == pytest.approx(origin)


def test_anchor_centre_lands_on_expected_pixels_for_offset_box():
    """An offset-origin box must not shift the anchor geometry."""
    anchors = _content_labels(OFFSET_BOX, [("RM-01", 0.10, 0.80, 120.0)])
    anchor = anchors["RM-01"]
    scale = DPI / 72.0
    centre_x_pt = OFFSET_BOX.x0 + 0.10 * OFFSET_BOX.width + 60.0
    centre_y_pt = OFFSET_BOX.y0 + 0.80 * OFFSET_BOX.height + HEIGHT_PT / 2.0
    assert anchor.x == pytest.approx((centre_x_pt - OFFSET_BOX.x0) * scale, abs=1e-6)
    assert anchor.y == pytest.approx(
        OFFSET_BOX.height * scale - (centre_y_pt - OFFSET_BOX.y0) * scale
    )
    assert anchor.width == pytest.approx(120.0 * scale)
    assert anchor.height == pytest.approx(HEIGHT_PT * scale)
    assert anchor.weight == pytest.approx((anchor.width * anchor.height) ** 0.5)


def test_offset_and_origin_box_pages_extract_identical_anchors():
    offset = _content_labels(OFFSET_BOX, OLD_CONTENT)
    origin = _content_labels(ORIGIN_BOX, OLD_CONTENT)
    assert set(offset) == set(origin)
    for text, anchor in offset.items():
        other = origin[text]
        assert anchor.x == pytest.approx(other.x)
        assert anchor.y == pytest.approx(other.y)
        assert anchor.width == pytest.approx(other.width)
        assert anchor.height == pytest.approx(other.height)


# ── Extraction ───────────────────────────────────────────────────────────


def test_extract_text_anchors_keeps_only_unique_body_labels():
    """Title block text, repeats, punctuation and single chars must all go."""
    anchors = _content_labels(OFFSET_BOX, OLD_CONTENT)
    assert set(anchors) == {"DOOR D1", "RM-01", "RM-02", "RM-03 3000", "RM-04", "RM-05"}
    texts = " ".join(anchors)
    # Title block words are excluded even though they are unique on the sheet:
    # they live at the same place on every revision, so they would align the
    # sheet frame instead of the drawing content.
    assert "PROJECT" not in texts
    assert "SCALE" not in texts
    assert "REV" not in texts
    assert "DRAWN" not in texts
    # Repeats of "TYP" and "1500" are ambiguous and must not become anchors.
    assert "TYP" not in texts
    assert "1500" not in texts
    for anchor in anchors.values():
        assert anchor.source == "text"
        assert len(anchor.text) >= 2


def test_title_zone_with_only_body_words_scores_zero_and_excludes_nothing():
    """A right-hand strip with no title-block vocabulary is ordinary content."""
    content = [("RM-01", 0.10, 0.80, 120.0), ("RM-02", 0.80, 0.60, 120.0)]
    page = _make_page(OFFSET_BOX, content, include_title=False)
    anchors = {anchor.text: anchor for anchor in extract_text_anchors(page, DPI)}
    assert set(anchors) == {"RM-01", "RM-02"}


def test_normalise_anchor_text():
    assert normalise_anchor_text("rm-01\u00a03000") == "RM-01 3000"
    assert normalise_anchor_text("  Door\u2003D1\t") == "DOOR D1"


# ── Matching ─────────────────────────────────────────────────────────────


def _anchor(text: str, x: float, y: float, width: float = 1.0, height: float = 1.0) -> Anchor:
    return Anchor(text=text, x=x, y=y, width=width, height=height)


def test_find_correspondences_reports_ambiguity_bookkeeping():
    """Duplicates on either side never match; the diagnostics say so."""
    old = [
        _anchor("RM-01", 100.0, 100.0, 200.0, 20.0),
        _anchor("RM-02", 100.0, 200.0, 200.0, 20.0),
        _anchor("RM-03", 100.0, 300.0, 200.0, 20.0),
        _anchor("RM-04", 100.0, 400.0, 200.0, 20.0),
        _anchor("RM-05", 100.0, 500.0, 200.0, 20.0),
        _anchor("TYP", 500.0, 500.0),
        _anchor("TYP", 600.0, 500.0),
        _anchor("1500", 300.0, 300.0, 90.0, 18.0),
    ]
    new = [
        _anchor("RM-01", 1100.0, 100.0, 200.0, 20.0),
        _anchor("RM-02", 1100.0, 200.0, 200.0, 20.0),
        _anchor("RM-03", 1100.0, 300.0, 200.0, 20.0),
        _anchor("RM-04", 1100.0, 400.0, 200.0, 20.0),
        _anchor("RM-05", 1100.0, 500.0, 200.0, 20.0),
        _anchor("RM-06", 1100.0, 600.0, 200.0, 20.0),
        _anchor("1500", 800.0, 800.0, 90.0, 18.0),
        _anchor("1500", 900.0, 900.0, 90.0, 18.0),
    ]
    result = find_correspondences(old, new)
    assert [correspondence.label for correspondence in result.correspondences] == [
        "RM-01",
        "RM-02",
        "RM-03",
        "RM-04",
        "RM-05",
    ]
    assert result.old_total == 8
    assert result.new_total == 8
    assert result.unique_matches == 5
    # "TYP" repeats on old, "1500" repeats on new: two ambiguous strings.
    assert result.discarded_ambiguous == 2
    # Weights come from the old anchor.
    first = result.correspondences[0]
    assert first.weight == pytest.approx((200.0 * 20.0) ** 0.5)
    assert (first.old_x, first.old_y) == (100.0, 100.0)
    assert (first.new_x, first.new_y) == (1100.0, 100.0)


def test_changed_label_drops_correspondence_end_to_end():
    """Two revisions of the same sheet: one label changed, content moved."""
    old_anchors = _content_labels(OFFSET_BOX, OLD_CONTENT)
    # The new revision renumbered room "DOOR D1" to "RM-06" and moved the
    # whole drawing 300 pt to the right; the title block stayed put.
    revised = [
        ("RM-06", fx, fy, width) if text == "DOOR D1" else (text, fx, fy, width)
        for text, fx, fy, width in OLD_CONTENT
    ]
    new_page = _make_page(OFFSET_BOX, revised, x_shift_pt=300.0)
    new_anchors = {anchor.text: anchor for anchor in extract_text_anchors(new_page, DPI)}

    old_list = list(old_anchors.values())
    new_list = list(new_anchors.values())
    result = find_correspondences(old_list, new_list)

    assert {c.label for c in result.correspondences} == {
        "RM-01",
        "RM-02",
        "RM-03 3000",
        "RM-04",
        "RM-05",
    }
    assert result.unique_matches == 5
    assert result.discarded_ambiguous == 0

    # The new-sheet copy of every matched label sits 300 pt further right
    # (300 pt at 200 dpi), on the same baseline.
    shift_px = 300.0 * DPI / 72.0
    for correspondence in result.correspondences:
        assert correspondence.new_x == pytest.approx(correspondence.old_x + shift_px)
        assert correspondence.new_y == pytest.approx(correspondence.old_y)


# ── Quality of the anchor set ────────────────────────────────────────────


def _correspondence(x: float, y: float) -> Correspondence:
    return Correspondence(old_x=x, old_y=y, new_x=x, new_y=y)


def test_assess_anchor_quality_corner_cluster_is_small():
    page_size = (3000.0, 2000.0)
    quality = assess_anchor_quality(
        [
            _correspondence(x, y)
            for x, y in [(120.0, 140.0), (180.0, 120.0), (220.0, 260.0), (300.0, 210.0)]
        ],
        page_size,
    )
    assert quality.count == 4
    assert quality.spread_fraction < 0.01
    assert quality.min_spacing_px > 50.0
    assert quality.max_spacing_px < 200.0


def test_assess_anchor_quality_spread_set_covers_the_page():
    page_size = (3000.0, 2000.0)
    corners = [
        _correspondence(x, y)
        for x, y in [(100.0, 100.0), (2900.0, 100.0), (2900.0, 1900.0), (100.0, 1900.0)]
    ]
    quality = assess_anchor_quality(corners, page_size)
    assert quality.spread_fraction == pytest.approx((2800.0 * 1800.0) / 6e6)
    assert quality.min_spacing_px == pytest.approx(1800.0)
    assert quality.max_spacing_px == pytest.approx(1800.0)
    # The corner cluster is orders of magnitude worse than the spread set.
    cluster = assess_anchor_quality(
        [
            _correspondence(x, y)
            for x, y in [(120.0, 140.0), (180.0, 120.0), (220.0, 260.0), (300.0, 210.0)]
        ],
        page_size,
    )
    assert quality.spread_fraction > cluster.spread_fraction * 50.0


def test_assess_anchor_quality_collinear_points_have_no_area():
    quality = assess_anchor_quality(
        [
            _correspondence(100.0, 100.0),
            _correspondence(1500.0, 300.0),
            _correspondence(2900.0, 500.0),
        ],
        (3000.0, 2000.0),
    )
    assert quality.spread_fraction == 0.0
    assert quality.min_spacing_px > 0.0


def test_assess_anchor_quality_two_points_have_spacing_but_no_spread():
    quality = assess_anchor_quality(
        [_correspondence(100.0, 100.0), _correspondence(400.0, 500.0)],
        (3000.0, 2000.0),
    )
    assert quality.spread_fraction == 0.0
    assert quality.min_spacing_px == pytest.approx(500.0)
    assert quality.max_spacing_px == pytest.approx(500.0)
