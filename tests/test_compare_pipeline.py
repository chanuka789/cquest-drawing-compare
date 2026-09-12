"""The comparison orchestrator: refusing, merging, clustering and reporting.

These tests are about the pipeline's judgement rather than any one stream:
what it refuses to do, what it merges, what it clusters, and what it records
so the run can be reproduced.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from engine.compare.orchestrator import (
    COMPARE_ENGINE_VERSION,
    AlignmentInput,
    CompareConfig,
    PairInput,
    SheetSource,
    cluster_regions,
    compare_batch,
    compare_pair,
    deduplicate,
    detect_dimension_lines,
)
from engine.compare.path_normalise import NormalisedPath
from engine.compare.tolerance import ToleranceSpec, resolve_for_sheet
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    Stream,
    TextCategory,
    TextChangeDetail,
)
from tests.harness.inject import ChangeDimension, ChangeRevisionLetter, ChangeSpec, inject_changes
from tests.phase5_cases import write_source

DPI = 150
TOLERANCE = resolve_for_sheet(ToleranceSpec(dpi=DPI), scale_text="1:100", dpi=DPI)


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_source(tmp_path_factory.mktemp("pipeline") / "source.pdf")


def compare(source: Path, spec: ChangeSpec, verdict: str = "good", **kwargs: object):
    pair = inject_changes(source, 0, spec, out_dir=source.parent)
    config = CompareConfig(dpi=DPI, tolerance=ToleranceSpec(dpi=DPI), **kwargs)  # type: ignore[arg-type]
    return compare_pair(
        PairInput(
            old=SheetSource(str(pair.old_path), 0, scale_text="1 : 100"),
            new=SheetSource(str(pair.new_path), 0, scale_text="1 : 100"),
            pair_id=spec.name,
        ),
        AlignmentInput(matrix=np.eye(3), verdict=verdict, dpi=DPI),
        config,
    )


# ── Refusing ────────────────────────────────────────────────────────────


def test_a_failed_alignment_is_refused_not_compared(source: Path):
    """A comparison of two sheets that did not align is wrong, not worse."""
    result = compare(source, ChangeSpec(name="refused"), verdict="failed")

    assert result.skipped
    assert result.changes == []
    assert "did not align" in result.skip_reason
    assert "manually" in result.skip_reason


def test_a_poor_alignment_can_be_overridden_by_the_user(source: Path):
    pair = inject_changes(source, 0, ChangeSpec(name="override"), out_dir=source.parent)
    result = compare_pair(
        PairInput(
            old=SheetSource(str(pair.old_path), 0, scale_text="1 : 100"),
            new=SheetSource(str(pair.new_path), 0, scale_text="1 : 100"),
            pair_id="override",
        ),
        AlignmentInput(matrix=np.eye(3), verdict="poor", user_override=True, dpi=DPI),
        CompareConfig(dpi=DPI, tolerance=ToleranceSpec(dpi=DPI)),
    )

    assert not result.skipped


# ── The gate ────────────────────────────────────────────────────────────


def test_the_revision_letter_alone_reports_nothing(source: Path):
    """The most important fixture in the project."""
    result = compare(source, ChangeSpec(name="gate", new=[ChangeRevisionLetter("C", "D")]))

    assert result.reportable == []
    assert not result.skipped


def test_the_run_records_everything_needed_to_reproduce_it(source: Path):
    result = compare(source, ChangeSpec(name="snapshot", new=[ChangeDimension("3000", "3200")]))

    assert result.engine_version == COMPARE_ENGINE_VERSION
    assert result.config_snapshot["dpi"] == DPI
    assert result.config_snapshot["tolerance"]["position_site_mm"] == 25.0
    assert result.tolerance is not None
    assert result.timings["text"] >= 0
    assert {stat.stream for stat in result.stream_stats} == {
        Stream.TEXT,
        Stream.VECTOR,
        Stream.HATCH,
        Stream.RASTER,
    }


def test_every_stream_says_whether_it_ran(source: Path):
    result = compare(source, ChangeSpec(name="streams"))
    for stat in result.stream_stats:
        assert stat.ran or stat.skip_reason, f"{stat.stream} neither ran nor said why"


# ── Deduplication across streams ────────────────────────────────────────


def text_change(box: Bbox) -> ChangeRecord:
    return ChangeRecord(
        kind=ChangeKind.MODIFIED,
        bbox=box,
        streams=[Stream.TEXT],
        description="Dimension changed: 3000 → 3200",
        confidence=0.8,
        text=TextChangeDetail(category=TextCategory.DIMENSION, old_text="3000", new_text="3200"),
    )


def raster_change(box: Bbox) -> ChangeRecord:
    return ChangeRecord(
        kind=ChangeKind.MODIFIED,
        bbox=box,
        streams=[Stream.RASTER],
        description="The drawing changed here",
        confidence=0.55,
    )


def test_the_same_change_seen_twice_is_one_record_with_both_streams():
    merged = deduplicate(
        [text_change(Bbox(100, 100, 40, 12)), raster_change(Bbox(95, 95, 60, 25))], 0.4
    )

    assert len(merged) == 1
    assert set(merged[0].streams) == {Stream.TEXT, Stream.RASTER}
    # The text stream's description survives: it is the informative one.
    assert "3000" in merged[0].description


def test_agreement_between_two_streams_raises_confidence():
    merged = deduplicate(
        [text_change(Bbox(100, 100, 40, 12)), raster_change(Bbox(95, 95, 60, 25))], 0.4
    )
    assert merged[0].confidence > 0.8


def test_two_separate_changes_stay_separate():
    merged = deduplicate(
        [text_change(Bbox(100, 100, 40, 12)), raster_change(Bbox(3000, 2000, 60, 25))], 0.4
    )
    assert len(merged) == 2


def test_two_findings_from_the_same_stream_are_never_merged():
    """Two dimensions that changed in one corner are two findings."""
    merged = deduplicate(
        [text_change(Bbox(100, 100, 40, 12)), text_change(Bbox(102, 102, 40, 12))], 0.4
    )
    assert len(merged) == 2


def test_a_sheet_level_record_is_never_merged_into_a_local_one():
    """Merging would turn a deliberate cosmetic verdict into a change."""
    layer = ChangeRecord(
        kind=ChangeKind.LAYER_VISIBILITY_CHANGED,
        bbox=Bbox(0, 0, 4000, 3000),
        streams=[Stream.VECTOR],
        is_cosmetic=True,
    )
    merged = deduplicate([layer, raster_change(Bbox(100, 100, 60, 25))], 0.4)

    assert len(merged) == 2
    assert any(change.is_cosmetic for change in merged)


# ── Clustering ──────────────────────────────────────────────────────────


def test_nearby_geometry_regions_become_one():
    changes = [raster_change(Bbox(100 + index * 10, 100, 8, 8)) for index in range(6)]
    clustered = cluster_regions(changes, TOLERANCE, cluster_paper_mm=5.0)

    assert len(clustered) == 1
    assert "6 objects" in clustered[0].description


def test_text_changes_are_never_clustered_away():
    """Each carries its own old and new value, which is the whole point."""
    changes = [text_change(Bbox(100, 100, 40, 12)), text_change(Bbox(105, 100, 40, 12))]
    clustered = cluster_regions(changes, TOLERANCE, cluster_paper_mm=5.0)

    assert len(clustered) == 2


def test_cosmetic_and_real_changes_never_cluster_together():
    real = raster_change(Bbox(100, 100, 8, 8))
    cosmetic = raster_change(Bbox(105, 100, 8, 8))
    cosmetic.is_cosmetic = True

    clustered = cluster_regions([real, cosmetic], TOLERANCE, cluster_paper_mm=5.0)

    assert len(clustered) == 2


def test_distant_regions_stay_apart():
    changes = [raster_change(Bbox(100, 100, 8, 8)), raster_change(Bbox(3000, 2000, 8, 8))]
    assert len(cluster_regions(changes, TOLERANCE, cluster_paper_mm=5.0)) == 2


# ── Context for the classifier ──────────────────────────────────────────


def line(x0: float, y0: float, x1: float, y1: float) -> NormalisedPath:
    import math

    points = [(x0, y0), (x1, y1)]
    return NormalisedPath(
        points=points,
        bbox=Bbox.from_points(points),
        length=math.hypot(x1 - x0, y1 - y0),
        point_count=2,
        geometry_type="line",
    )


def test_a_line_with_ticks_at_both_ends_is_a_dimension_line():
    px = TOLERANCE.px_per_mm
    paths = [
        line(100, 200, 100 + 50 * px, 200),  # the dimension line
        line(100, 200 - 2 * px, 100, 200 + 2 * px),  # a tick at each end
        line(100 + 50 * px, 200 - 2 * px, 100 + 50 * px, 200 + 2 * px),
    ]
    found = detect_dimension_lines(paths, TOLERANCE)

    assert len(found) == 1


def test_a_bare_line_is_not_a_dimension_line():
    px = TOLERANCE.px_per_mm
    assert detect_dimension_lines([line(100, 200, 100 + 50 * px, 200)], TOLERANCE) == []


# ── Batches ─────────────────────────────────────────────────────────────


def test_a_batch_reports_progress_and_can_resume(source: Path):
    pairs = []
    for name in ("batch-a", "batch-b"):
        injected = inject_changes(source, 0, ChangeSpec(name=name), out_dir=source.parent)
        pairs.append(
            (
                PairInput(
                    old=SheetSource(str(injected.old_path), 0, scale_text="1 : 100"),
                    new=SheetSource(str(injected.new_path), 0, scale_text="1 : 100"),
                    pair_id=name,
                ),
                AlignmentInput(matrix=np.eye(3), verdict="good", dpi=DPI),
            )
        )

    seen: list[tuple[int, int, str]] = []
    config = CompareConfig(dpi=DPI, tolerance=ToleranceSpec(dpi=DPI))
    results = compare_batch(
        pairs, config, lambda done, total, label: seen.append((done, total, label)), workers=1
    )

    assert len(results) == 2
    assert seen[-1][0] == 2

    # Resuming skips what is already done.
    resumed = compare_batch(pairs, config, workers=1, already_done={"batch-a"})
    assert [result.pair_id for result in resumed] == ["batch-b"]


def test_a_batch_stops_when_cancelled(source: Path):
    pairs = []
    for name in ("cancel-a", "cancel-b", "cancel-c"):
        injected = inject_changes(source, 0, ChangeSpec(name=name), out_dir=source.parent)
        pairs.append(
            (
                PairInput(
                    old=SheetSource(str(injected.old_path), 0, scale_text="1 : 100"),
                    new=SheetSource(str(injected.new_path), 0, scale_text="1 : 100"),
                    pair_id=name,
                ),
                AlignmentInput(matrix=np.eye(3), verdict="good", dpi=DPI),
            )
        )

    results = compare_batch(
        pairs,
        CompareConfig(dpi=DPI, tolerance=ToleranceSpec(dpi=DPI)),
        workers=1,
        should_cancel=lambda: True,
    )
    assert results == []
