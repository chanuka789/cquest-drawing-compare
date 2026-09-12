"""Hatch regions and the vector diff.

The hatch rule is the one that decides whether a report is readable: a wall's
hatch is two thousand segments and a material change replaces every one of
them. One region, one record.
"""

from __future__ import annotations

import math

import pytest

from engine.compare.hatch import (
    detect_hatch_regions,
    diff_hatch,
    exclude_hatch_paths,
)
from engine.compare.path_normalise import NormalisedPath, geometry_hash, style_hash
from engine.compare.tolerance import ToleranceSpec, resolve_for_sheet
from engine.compare.types import Bbox, ChangeKind, Stream
from engine.compare.vector_diff import VectorDiffConfig, diff_vectors

TOLERANCE = resolve_for_sheet(ToleranceSpec(dpi=200), scale_text="1:100", dpi=200)
PX_PER_MM = TOLERANCE.px_per_mm


def segment(x0: float, y0: float, x1: float, y1: float, **state: object) -> NormalisedPath:
    points = [(x0, y0), (x1, y1)]
    return NormalisedPath(
        points=points,
        geometry_hash=geometry_hash(points, False, 1.0),
        style_hash=style_hash_for(state),
        bbox=Bbox.from_points(points),
        length=math.hypot(x1 - x0, y1 - y0),
        point_count=2,
        geometry_type="line",
        line_width_px=float(state.get("line_width_px", 1.0)),
        dash_array=tuple(state.get("dash_array", ())),  # type: ignore[arg-type]
    )


def style_hash_for(state: dict[str, object]) -> str:
    from engine.extract.vector_extractor import RawPath

    return style_hash(
        RawPath(
            line_width_px=float(state.get("line_width_px", 1.0)),
            dash_array=tuple(state.get("dash_array", ())),  # type: ignore[arg-type]
        )
    )


def hatched_wall(
    *,
    x0: float = 100.0,
    y0: float = 100.0,
    length: float = 4000.0,
    thickness: float = 24.0,
    angle_deg: float = 45.0,
    spacing_mm: float = 2.0,
    count: int = 80,
) -> list[NormalisedPath]:
    """A wall hatched the way a plotter draws one: short, fine, regular."""
    spacing = spacing_mm * PX_PER_MM
    step = spacing / max(math.sin(math.radians(angle_deg)) or 1.0, 0.1)
    direction = (math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg)))
    segments: list[NormalisedPath] = []
    for index in range(count):
        start_x = x0 + index * step
        if start_x > x0 + length:
            break
        span = thickness / max(abs(direction[1]), 0.1)
        segments.append(segment(start_x, y0, start_x + direction[0] * span, y0 + thickness))
    return segments


# ── Detection ───────────────────────────────────────────────────────────


def test_a_hatched_wall_is_one_region():
    regions = detect_hatch_regions(hatched_wall(), TOLERANCE)

    assert len(regions) == 1
    region = regions[0]
    assert region.segment_count >= 20
    assert region.spacing_px / PX_PER_MM == pytest.approx(2.0, abs=0.5)


def test_a_setting_out_grid_is_not_hatch():
    """Regular and parallel, but drawn far too coarsely to be a texture."""
    grid = [
        segment(100 + index * 60 * PX_PER_MM, 100, 100 + index * 60 * PX_PER_MM, 2000)
        for index in range(30)
    ]
    assert detect_hatch_regions(grid, TOLERANCE) == []


def test_irregular_spacing_is_not_hatch():
    """A stair or a run of joists is dense and parallel but not regular."""
    irregular = [
        segment(100 + offset, 100, 100 + offset + 20, 124)
        for offset in (
            0,
            7,
            30,
            33,
            70,
            71,
            130,
            190,
            191,
            250,
            320,
            322,
            400,
            402,
            480,
            559,
            560,
            640,
            700,
            780,
            800,
            900,
            1000,
            1100,
        )
    ]
    assert detect_hatch_regions(irregular, TOLERANCE) == []


def test_too_few_segments_is_not_hatch():
    assert detect_hatch_regions(hatched_wall(count=8), TOLERANCE) == []


def test_hatch_segments_are_withheld_from_the_vector_diff():
    """Counted twice, a hatch region is also two thousand line changes."""
    paths = hatched_wall()
    regions = detect_hatch_regions(paths, TOLERANCE)
    remaining = exclude_hatch_paths(paths, regions, TOLERANCE)

    assert len(remaining) == 0


def test_a_stray_segment_inside_a_region_is_withheld_too():
    """The outermost segment often fails to join the cluster as noise.

    Left in, it arrives in the report on its own as a line that appeared out
    of nowhere in the middle of a wall.
    """
    paths = hatched_wall()
    regions = detect_hatch_regions(paths, TOLERANCE)
    stray = segment(110, 105, 118, 120)
    remaining = exclude_hatch_paths([*paths, stray], regions, TOLERANCE)

    assert remaining == []


# ── Comparison ──────────────────────────────────────────────────────────


def test_a_pattern_change_is_one_record_not_thousands():
    old = detect_hatch_regions(hatched_wall(angle_deg=45.0), TOLERANCE)
    new = detect_hatch_regions(hatched_wall(angle_deg=90.0), TOLERANCE)

    changes = diff_hatch(old, new, TOLERANCE)

    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.HATCH_PATTERN_CHANGED
    assert changes[0].streams == [Stream.HATCH]
    assert "different material" in changes[0].description


def test_an_unchanged_region_reports_nothing():
    regions = detect_hatch_regions(hatched_wall(), TOLERANCE)
    assert diff_hatch(regions, regions, TOLERANCE) == []


def test_a_region_only_in_the_new_sheet_was_added():
    new = detect_hatch_regions(hatched_wall(), TOLERANCE)
    changes = diff_hatch([], new, TOLERANCE)

    assert [change.kind for change in changes] == [ChangeKind.HATCH_ADDED]
    assert "segments as one region" in changes[0].description


def test_a_region_only_in_the_old_sheet_was_removed():
    old = detect_hatch_regions(hatched_wall(), TOLERANCE)
    changes = diff_hatch(old, [], TOLERANCE)

    assert [change.kind for change in changes] == [ChangeKind.HATCH_REMOVED]


def test_hatch_changes_carry_their_area_in_square_metres():
    old = detect_hatch_regions(hatched_wall(angle_deg=45.0), TOLERANCE)
    new = detect_hatch_regions(hatched_wall(angle_deg=90.0), TOLERANCE)
    change = diff_hatch(old, new, TOLERANCE)[0]

    assert change.hatch is not None
    assert change.hatch.new_area_m2 is not None
    assert change.hatch.old_signature != change.hatch.new_signature


# ── The vector diff ─────────────────────────────────────────────────────


def test_identical_geometry_reports_nothing():
    paths = [segment(0, 0, 100, 0), segment(100, 0, 100, 100)]
    result = diff_vectors(paths, list(paths), TOLERANCE)

    assert result.changes == []
    assert result.unchanged_count == 2


def test_a_new_path_is_an_addition():
    old = [segment(0, 0, 100, 0)]
    new = [segment(0, 0, 100, 0), segment(500, 500, 900, 500)]
    result = diff_vectors(old, new, TOLERANCE)

    assert [change.kind for change in result.changes] == [ChangeKind.ADDED]


def test_the_same_shape_nearby_is_a_move_not_a_removal_and_an_addition():
    old = [segment(0, 0, 400, 0)]
    new = [segment(0, 120, 400, 120)]
    result = diff_vectors(old, new, TOLERANCE)

    assert [change.kind for change in result.changes] == [ChangeKind.MOVED]
    assert result.moved_count == 1


def test_the_same_geometry_with_a_different_pen_is_cosmetic():
    old = [segment(0, 0, 400, 0, line_width_px=1.0)]
    new = [segment(0, 0, 400, 0, line_width_px=4.0)]
    result = diff_vectors(old, new, TOLERANCE)

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind is ChangeKind.STYLE_ONLY
    assert change.is_cosmetic
    assert "line weight" in change.description


def test_a_scanned_sheet_skips_the_stream_and_records_why():
    result = diff_vectors([], [], TOLERANCE, skip_reason="This sheet is a scan.")

    assert not result.ran
    assert result.skip_reason == "This sheet is a scan."
    assert result.changes == []


def test_style_only_reporting_can_be_switched_off():
    old = [segment(0, 0, 400, 0, line_width_px=1.0)]
    new = [segment(0, 0, 400, 0, line_width_px=4.0)]
    config = VectorDiffConfig(report_style_only=False)
    result = diff_vectors(old, new, TOLERANCE, config=config)

    assert result.changes == []
    assert result.style_only_count == 1
