"""Stage 6 unit tests: the diff, the clustering and the change typing.

These work on synthetic rasters rather than on PDFs, so every expectation
is exact: the test draws the change it wants found, and then checks that
exactly that change came back. Rendering is covered separately.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from engine.align.units import mm_to_px
from engine.compare.change_typer import build_regions
from engine.compare.cluster import cluster_regions, merge_boxes, sort_boxes
from engine.compare.exclusions import ExclusionZone, build_mask
from engine.compare.raster_diff import diff_masks, ink_mask, tolerance_radius_px
from engine.compare.types import CompareConfig
from engine.core.enums import ChangeType, Severity

DPI = 200
SIZE = (1200, 1600)  # height, width — a small sheet, fast to diff


def blank() -> NDArray[np.uint8]:
    """An empty white sheet."""
    return np.full(SIZE, 255, dtype=np.uint8)


def with_box(
    sheet: NDArray[np.uint8],
    x: int,
    y: int,
    w: int,
    h: int,
    thickness: int = -1,
) -> NDArray[np.uint8]:
    """A filled black rectangle drawn onto a copy of *sheet*."""
    out = sheet.copy()
    cv2.rectangle(out, (x, y), (x + w, y + h), 0, thickness)
    return out


@pytest.fixture
def config() -> CompareConfig:
    return CompareConfig(dpi=DPI)


# ── The raster diff ────────────────────────────────────────────────────


def test_identical_sheets_have_no_difference(config: CompareConfig) -> None:
    sheet = with_box(blank(), 400, 300, 200, 150)
    masks = diff_masks(sheet, sheet, None, config)
    assert masks.added_px == 0
    assert masks.removed_px == 0


def test_new_ink_reads_as_added(config: CompareConfig) -> None:
    old = blank()
    new = with_box(old, 400, 300, 200, 150)
    masks = diff_masks(old, new, None, config)
    assert masks.added_px > 0
    assert masks.removed_px == 0


def test_vanished_ink_reads_as_removed(config: CompareConfig) -> None:
    new = blank()
    old = with_box(new, 400, 300, 200, 150)
    masks = diff_masks(old, new, None, config)
    assert masks.removed_px > 0
    assert masks.added_px == 0


def test_drift_inside_the_tolerance_is_not_a_change(config: CompareConfig) -> None:
    """A stroke that moved less than the tolerance is still the same stroke."""
    old = with_box(blank(), 400, 300, 200, 150, thickness=3)
    drift = max(1, tolerance_radius_px(config) - 1)
    new = with_box(blank(), 400 + drift, 300, 200, 150, thickness=3)
    masks = diff_masks(old, new, None, config)
    assert masks.added_px == 0
    assert masks.removed_px == 0


def test_drift_beyond_the_tolerance_is_a_change(config: CompareConfig) -> None:
    old = with_box(blank(), 400, 300, 200, 150, thickness=3)
    shift = tolerance_radius_px(config) * 6
    new = with_box(blank(), 400 + shift, 300, 200, 150, thickness=3)
    masks = diff_masks(old, new, None, config)
    assert masks.added_px > 0
    assert masks.removed_px > 0


def test_translation_matrix_is_undone_before_diffing(config: CompareConfig) -> None:
    """The alignment transform is applied, so a shifted sheet is unchanged."""
    old = with_box(blank(), 400, 300, 200, 150, thickness=3)
    shift = 37
    new = with_box(blank(), 400 + shift, 300 + shift, 200, 150, thickness=3)
    matrix = np.array(
        [[1.0, 0.0, float(shift)], [0.0, 1.0, float(shift)], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    masks = diff_masks(old, new, matrix, config)
    assert masks.added_px == 0
    assert masks.removed_px == 0


def test_area_outside_the_warped_old_sheet_is_never_a_change(
    config: CompareConfig,
) -> None:
    """Uncovered area is unknown, not removed."""
    old = blank()
    new = with_box(blank(), 30, 30, 60, 60)
    # Push the old sheet almost entirely off the new grid.
    matrix = np.array([[1.0, 0.0, 1000.0], [0.0, 1.0, 800.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    masks = diff_masks(old, new, matrix, config)
    # The drawn box sits in the uncovered corner, so it cannot be reported.
    assert masks.added_px == 0


def test_exclusion_mask_suppresses_its_zone(config: CompareConfig) -> None:
    """The point of the title block mask: a change inside it is not reported."""
    old = blank()
    new = with_box(old, 1300, 1000, 200, 150)
    zone = ExclusionZone(
        name="bottom_right",
        x_from=0.7,
        y_from=0.7,
        x_to=1.0,
        y_to=1.0,
        reason="test",
    )
    mask = build_mask([zone], SIZE[1], SIZE[0])
    assert mask is not None
    masks = diff_masks(old, new, None, config, exclusion_mask=mask)
    assert masks.added_px == 0


def test_ink_mask_is_threshold_not_otsu() -> None:
    """A near-empty sheet must not have its faintest smudge promoted to ink."""
    sheet = np.full((10, 10), 250, dtype=np.uint8)
    assert np.count_nonzero(ink_mask(sheet)) == 0


# ── Clustering ─────────────────────────────────────────────────────────


def test_one_change_is_one_region(config: CompareConfig) -> None:
    old = blank()
    new = with_box(old, 400, 300, 200, 150)
    masks = diff_masks(old, new, None, config)
    boxes = cluster_regions(masks, config)
    assert len(boxes) == 1


def test_two_distant_changes_stay_two_regions(config: CompareConfig) -> None:
    old = blank()
    new = with_box(with_box(old, 100, 100, 120, 120), 1200, 900, 120, 120)
    masks = diff_masks(old, new, None, config)
    assert len(cluster_regions(masks, config)) == 2


def test_specks_below_the_noise_floor_are_dropped(config: CompareConfig) -> None:
    old = blank()
    new = old.copy()
    new[500, 500] = 0  # a single pixel of dust
    masks = diff_masks(old, new, None, config)
    assert cluster_regions(masks, config) == []


def test_merge_boxes_unions_touching_boxes() -> None:
    merged = merge_boxes([(0, 0, 10, 10), (12, 0, 10, 10)], pad=5)
    assert merged == [(0, 0, 22, 10)]


def test_merge_boxes_leaves_distant_boxes_alone() -> None:
    assert len(merge_boxes([(0, 0, 10, 10), (500, 500, 10, 10)], pad=5)) == 2


def test_sort_boxes_is_reading_order() -> None:
    boxes = [(500, 900, 5, 5), (10, 20, 5, 5), (900, 20, 5, 5)]
    assert sort_boxes(boxes) == [(10, 20, 5, 5), (900, 20, 5, 5), (500, 900, 5, 5)]


# ── Change typing ──────────────────────────────────────────────────────


def _regions(old: NDArray[np.uint8], new: NDArray[np.uint8], config: CompareConfig):
    masks = diff_masks(old, new, None, config)
    boxes = sort_boxes(cluster_regions(masks, config))
    return build_regions(boxes, masks, config, scale_denominator=100)


def test_added_content_is_typed_added(config: CompareConfig) -> None:
    regions = _regions(blank(), with_box(blank(), 400, 300, 200, 150), config)
    assert len(regions) == 1
    assert regions[0].change_type is ChangeType.ADDED


def test_removed_content_is_typed_removed(config: CompareConfig) -> None:
    regions = _regions(with_box(blank(), 400, 300, 200, 150), blank(), config)
    assert len(regions) == 1
    assert regions[0].change_type is ChangeType.REMOVED


def test_redrawn_content_is_typed_modified(config: CompareConfig) -> None:
    """Ink both gone and arrived in the same place is a modification."""
    old = with_box(blank(), 400, 300, 200, 150, thickness=4)
    new = with_box(blank(), 400, 300, 260, 150, thickness=4)
    regions = _regions(old, new, config)
    assert any(region.change_type is ChangeType.MODIFIED for region in regions)


def test_content_that_moved_is_typed_moved(config: CompareConfig) -> None:
    """The same shape, elsewhere on the sheet, is one move — not two changes.

    Drawn as outlines, because that is what a drawing is: a solid block of
    ink carries no structure for the match to lock onto.
    """
    old = with_box(blank(), 200, 200, 160, 120, thickness=3)
    new = with_box(blank(), 800, 700, 160, 120, thickness=3)
    regions = _regions(old, new, config)
    assert any(region.change_type is ChangeType.MOVED for region in regions)
    moved = next(r for r in regions if r.change_type is ChangeType.MOVED)
    assert moved.moved_by_mm is not None and moved.moved_by_mm > 0
    assert moved.moved_from_px is not None


def test_measurements_are_reported_in_millimetres(config: CompareConfig) -> None:
    regions = _regions(blank(), with_box(blank(), 400, 300, 200, 150), config)
    region = regions[0]
    x_mm, _y_mm, w_mm, _h_mm = region.bbox_mm
    assert x_mm == pytest.approx(400 / mm_to_px(1.0, DPI), rel=0.05)
    assert w_mm == pytest.approx(200 / mm_to_px(1.0, DPI), rel=0.05)
    assert region.area_mm2 > 0


def test_site_measurements_use_the_drawing_scale(config: CompareConfig) -> None:
    """At 1:100, one paper mm² is 10 000 site mm²."""
    regions = _regions(blank(), with_box(blank(), 400, 300, 200, 150), config)
    region = regions[0]
    assert region.area_site_mm2 == pytest.approx(region.area_mm2 * 100 * 100)


def test_no_scale_means_no_site_measurement(config: CompareConfig) -> None:
    masks = diff_masks(blank(), with_box(blank(), 400, 300, 200, 150), None, config)
    boxes = sort_boxes(cluster_regions(masks, config))
    regions = build_regions(boxes, masks, config, scale_denominator=None)
    assert regions[0].area_site_mm2 is None


def test_a_tiny_change_is_cosmetic_and_trivial(config: CompareConfig) -> None:
    old = blank()
    new = with_box(old, 400, 300, 12, 12)
    regions = _regions(old, new, config)
    assert regions and regions[0].is_cosmetic
    assert regions[0].severity is Severity.TRIVIAL


def test_a_large_removal_is_critical(config: CompareConfig) -> None:
    """Removals outrank additions: they are what gets built by mistake."""
    old = with_box(blank(), 200, 200, 900, 700)
    regions = _regions(old, blank(), config)
    assert regions
    assert regions[0].severity is Severity.CRITICAL


def test_every_region_explains_itself(config: CompareConfig) -> None:
    regions = _regions(blank(), with_box(blank(), 400, 300, 200, 150), config)
    explanation = regions[0].explanation
    assert explanation
    assert "px" not in explanation.lower()  # never pixels, in front of a user
    assert "mm" in explanation
