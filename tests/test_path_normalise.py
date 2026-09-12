"""Path normalisation invariants.

These are the properties the vector stream is built on. If any of them fails,
a sheet replotted from the same model reports every object on it as removed
and re-added — the worst output the tool can produce.
"""

from __future__ import annotations

import math

import pytest

from engine.compare.path_normalise import (
    canonicalise,
    classify_geometry,
    flatten_cubic,
    geometry_hash,
    merge_collinear,
    normalise_path,
    path_length,
    quantise,
    style_hash,
    transform_points,
)
from engine.compare.types import Bbox
from engine.extract.vector_extractor import (
    SEGMENT_LINETO,
    SEGMENT_MOVETO,
    RawPath,
    Segment,
)

PX_PER_MM = 200 / 25.4
GRID = 1.0


def line_path(points: list[tuple[float, float]], **state: object) -> RawPath:
    segments = [Segment(SEGMENT_MOVETO, [points[0]])]
    segments += [Segment(SEGMENT_LINETO, [point]) for point in points[1:]]
    return RawPath(
        segments=segments,
        stroked=True,
        bbox=Bbox.from_points(points),
        **state,  # type: ignore[arg-type]
    )


def only(path: RawPath):
    results = normalise_path(path, tolerance_px=GRID, px_per_mm=PX_PER_MM)
    assert len(results) == 1
    return results[0]


# ── The invariants ──────────────────────────────────────────────────────


def test_a_reversed_path_hashes_identically():
    """A path from A to B is the same line as the same path from B to A."""
    forward = only(line_path([(10, 10), (100, 10), (100, 90)]))
    backward = only(line_path([(100, 90), (100, 10), (10, 10)]))

    assert forward.geometry_hash == backward.geometry_hash


def test_a_line_split_into_three_hashes_as_one_line():
    """One wall drawn as three collinear segments is one wall."""
    single = only(line_path([(0, 0), (300, 0)]))
    split = only(line_path([(0, 0), (100, 0), (200, 0), (300, 0)]))

    assert single.geometry_hash == split.geometry_hash
    assert split.point_count == 2


def test_a_closed_ring_hashes_the_same_from_any_start_point():
    square = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
    rotated = [(100, 100), (0, 100), (0, 0), (100, 0), (100, 100)]

    first = canonicalise(square, closed=True)
    second = canonicalise(rotated, closed=True)

    assert geometry_hash(first, True, GRID) == geometry_hash(second, True, GRID)


def test_two_curves_that_render_identically_flatten_identically():
    """Different control points, same drawn curve, same hash."""
    # A straight line expressed as a cubic, two ways.
    first = flatten_cubic((0, 0), (10, 0), (20, 0), (30, 0), tolerance=0.1)
    second = flatten_cubic((0, 0), (15, 0), (25, 0), (30, 0), tolerance=0.1)

    assert first == second == [(30, 0)]


def test_a_tight_curve_is_subdivided_until_it_is_flat():
    points = flatten_cubic((0, 0), (0, 100), (100, 100), (100, 0), tolerance=0.5)

    assert len(points) > 4
    assert points[-1] == (100, 0)
    # Every point sits on the curve's hull, so the polyline never overshoots.
    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in points)


def test_coordinates_are_quantised_to_the_tolerance_grid():
    """A shift smaller than the grid must not change the hash."""
    exact = only(line_path([(10.0, 10.0), (100.0, 10.0)]))
    jittered = only(line_path([(10.2, 10.1), (100.1, 9.9)]))

    assert exact.geometry_hash == jittered.geometry_hash


def test_style_never_reaches_the_geometry_hash():
    """A heavier pen is the same line. That is what makes replots cosmetic."""
    thin = only(line_path([(0, 0), (100, 0)], line_width_px=1.0))
    thick = only(line_path([(0, 0), (100, 0)], line_width_px=4.0, dash_array=(3.0, 2.0)))

    assert thin.geometry_hash == thick.geometry_hash
    assert thin.style_hash != thick.style_hash


def test_style_hash_covers_weight_dash_and_colour():
    base = line_path([(0, 0), (100, 0)])
    assert style_hash(base) == style_hash(line_path([(0, 0), (100, 0)]))
    assert style_hash(base) != style_hash(line_path([(0, 0), (100, 0)], line_width_px=2.0))
    assert style_hash(base) != style_hash(line_path([(0, 0), (100, 0)], dash_array=(2.0,)))
    assert style_hash(base) != style_hash(
        line_path([(0, 0), (100, 0)], stroke_colour=(255, 0, 0, 255))
    )


# ── Pieces ──────────────────────────────────────────────────────────────


def test_merge_collinear_keeps_real_corners():
    points = [(0, 0), (100, 0), (200, 0), (200, 100)]
    merged = merge_collinear(points)

    assert merged == [(0, 0), (200, 0), (200, 100)]


def test_merge_collinear_respects_the_angular_tolerance():
    """A quarter-degree kink is plotting noise; ten degrees is a corner."""
    slight = [(0, 0), (100, 0), (200, math.tan(math.radians(0.25)) * 100)]
    sharp = [(0, 0), (100, 0), (200, math.tan(math.radians(10)) * 100)]

    assert len(merge_collinear(slight)) == 2
    assert len(merge_collinear(sharp)) == 3


def test_quantise_snaps_to_the_grid():
    assert quantise([(10.4, 10.6)], 1.0) == [(10.0, 11.0)]
    assert quantise([(10.4, 10.6)], 0.0) == [(10.4, 10.6)]


def test_geometry_types_are_named_for_the_report():
    assert classify_geometry([(0, 0), (100, 0)], False, False) == "line"
    square = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
    assert classify_geometry(square, True, False) == "rectangle"
    circle = [
        (math.cos(t) * 50 + 50, math.sin(t) * 50 + 50) for t in [i * math.pi / 6 for i in range(12)]
    ]
    assert classify_geometry([*circle, circle[0]], True, True) == "circle"


def test_degenerate_paths_are_dropped():
    """A zero-length path draws nothing and must not make sheets differ."""
    assert (
        normalise_path(line_path([(10, 10), (10, 10)]), tolerance_px=GRID, px_per_mm=PX_PER_MM)
        == []
    )


def test_length_is_measured_along_the_path():
    assert path_length([(0, 0), (30, 0), (30, 40)]) == pytest.approx(70.0)


def test_points_map_through_the_alignment_matrix():
    matrix = [[1.0, 0.0, 100.0], [0.0, 1.0, 50.0], [0.0, 0.0, 1.0]]
    assert transform_points([(10, 10)], matrix) == [(110.0, 60.0)]
