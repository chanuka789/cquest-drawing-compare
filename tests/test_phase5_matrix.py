"""Phase 5's definition of done, measured rather than asserted by eye.

The gate is first and it is not negotiable: **`rev_letter_only` must report
zero changes.** Every noise filter in the phase is measured against it, and
the day it returns zero is the day the phase is genuinely working.

After that come the cosmetic-only suite, precision and recall across the
injection matrix, and the specific behaviours the plan calls out: one record
for a hatch region, one for a renumbering, one for a toggled layer, and a
warning rather than a change list when the alignment was not tight enough.

The golden file is the regression guard. Any change that lowers precision or
raises the false positive count fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from engine.compare.orchestrator import (
    AlignmentInput,
    CompareConfig,
    ComparisonResult,
    PairInput,
    SheetSource,
    compare_pair,
)
from engine.compare.tolerance import ToleranceSpec
from engine.compare.types import ChangeKind, Stream
from tests.harness.inject import InjectedPair
from tests.harness.precision_recall import MatrixReport, run_case, score_pair
from tests.phase5_cases import all_cases, cosmetic_cases, genuine_cases, write_source

#: The suite runs at 150 DPI: the comparison is resolution independent and
#: this keeps twenty full pipeline runs inside a sensible test time.
DPI = 150

GOLDEN = Path(__file__).with_name("phase5_golden.json")

#: The plan's targets.
MIN_PRECISION = 0.90
MIN_RECALL = 0.85
#: Time budget for one full comparison of one pair, in seconds.
PAIR_BUDGET_S = 8.0


@pytest.fixture(scope="module")
def config() -> CompareConfig:
    return CompareConfig(dpi=DPI, tolerance=ToleranceSpec(dpi=DPI))


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("phase5")
    write_source(root / "source.pdf")
    return root


@pytest.fixture(scope="module")
def matrix(
    workspace: Path, config: CompareConfig
) -> dict[str, tuple[InjectedPair, ComparisonResult]]:
    """Every case, run once, shared by the whole module."""
    source = workspace / "source.pdf"
    results: dict[str, tuple[InjectedPair, ComparisonResult]] = {}
    for case in all_cases():
        pair, result, _metrics = run_case(source, case.spec, workspace, config=config)
        results[case.name] = (pair, result)
    return results


def report_for(matrix: dict[str, tuple[InjectedPair, ComparisonResult]]) -> MatrixReport:
    report = MatrixReport()
    for pair, result in matrix.values():
        report.pairs.append(score_pair(pair, result, DPI))
    return report


# ── The gate ────────────────────────────────────────────────────────────


def test_rev_letter_only_reports_zero_changes(matrix):
    """The gate for the whole phase. If this fails nothing else matters."""
    _pair, result = matrix["rev_letter_only"]

    assert result.reportable == [], [change.description for change in result.reportable]


@pytest.mark.parametrize("name", [case.name for case in cosmetic_cases()])
def test_cosmetic_only_pairs_report_no_real_changes(matrix, name):
    _pair, result = matrix[name]

    assert result.reportable == [], [change.description for change in result.reportable]


def test_the_false_positive_count_on_cosmetic_pairs_is_zero(matrix):
    """The headline metric of Phase 5."""
    assert report_for(matrix).cosmetic_false_positives == 0


# ── Accuracy ────────────────────────────────────────────────────────────


def test_precision_and_recall_meet_the_plans_targets(matrix):
    report = report_for(matrix)

    assert report.precision >= MIN_PRECISION, report.table()
    assert report.recall >= MIN_RECALL, report.table()


@pytest.mark.parametrize("name", [case.name for case in genuine_cases()])
def test_every_genuine_change_is_found(matrix, name):
    pair, result = matrix[name]
    metrics = score_pair(pair, result, DPI)

    assert metrics.missed == 0, f"{name} missed {metrics.missed_labels}"


def test_real_small_finds_its_changes_with_no_extras(matrix):
    pair, result = matrix["real_small"]
    metrics = score_pair(pair, result, DPI)

    assert metrics.covered == 3
    assert metrics.false_positives == 0


def test_real_heavy_stays_readable(matrix):
    """A heavily revised sheet must still read as a list, not a mess."""
    pair, result = matrix["real_heavy"]
    metrics = score_pair(pair, result, DPI)

    assert metrics.missed == 0
    assert len(result.reportable) <= 20


# ── Specific behaviours ─────────────────────────────────────────────────


def test_a_dimension_change_is_described_the_way_a_surveyor_reads_it(matrix):
    _pair, result = matrix["dimension_only"]
    change = result.reportable[0]

    assert change.text is not None
    assert change.text.old_text == "3000"
    assert change.text.new_text == "3200"
    assert "+200 mm" in change.description
    assert "6.7%" in change.description


def test_a_dimension_that_changed_alone_is_flagged_as_such(matrix):
    _pair, result = matrix["dimension_only"]
    change = result.reportable[0]

    assert change.text is not None
    assert str(change.text.cross_check) == "dimension_text_only"
    assert "not to scale" in change.description


def test_a_level_change_is_reported_in_millimetres(matrix):
    _pair, result = matrix["level_change"]
    change = result.reportable[0]

    assert "+150 mm" in change.description


def test_a_hatch_region_produces_one_record_not_thousands(matrix):
    _pair, result = matrix["hatch_change"]
    hatch = [change for change in result.reportable if Stream.HATCH in change.streams]

    assert len(hatch) == 1
    assert hatch[0].kind is ChangeKind.HATCH_PATTERN_CHANGED
    assert "different material" in hatch[0].description
    assert len(result.reportable) <= 2


def test_tag_renumbering_produces_one_record_with_the_mapping(matrix):
    _pair, result = matrix["renumbered"]
    renumbering = [
        change for change in result.reportable if change.kind is ChangeKind.TAGS_RENUMBERED
    ]

    assert len(renumbering) == 1
    assert len(result.reportable) == 1
    mapping = renumbering[0].detail["mapping"]
    assert mapping["D-12"] == "D-22"
    assert len(mapping) == 4


def test_a_toggled_layer_produces_one_record_naming_the_layer(matrix):
    _pair, result = matrix["layer_toggled"]
    layers = [
        change for change in result.changes if change.kind is ChangeKind.LAYER_VISIBILITY_CHANGED
    ]

    assert len(layers) == 1
    assert "SETTING OUT" in layers[0].description
    assert layers[0].is_cosmetic


def test_a_replot_collapses_into_one_cosmetic_record(matrix):
    _pair, result = matrix["line_weight"]

    assert result.reportable == []
    assert len(result.cosmetic) == 1
    assert "pen weight" in result.cosmetic[0].description


def test_a_real_change_survives_a_watermark_and_a_replot(matrix):
    for name in ("change_under_watermark", "change_with_replot"):
        _pair, result = matrix[name]
        assert len(result.reportable) == 1, name
        assert result.reportable[0].text is not None, name


# ── Alignment residual ──────────────────────────────────────────────────


def test_a_misaligned_pair_warns_instead_of_reporting_changes(
    workspace: Path, config: CompareConfig
):
    """Two sheets shifted by a pixel must not report every stroke as changed."""
    from tests.harness.inject import ChangeSpec, inject_changes

    pair = inject_changes(
        workspace / "source.pdf", 0, ChangeSpec(name="misaligned"), out_dir=workspace
    )
    # An alignment matrix that is deliberately one pixel out.
    matrix = np.eye(3)
    matrix[0, 2] = 1.0
    matrix[1, 2] = 1.0

    result = compare_pair(
        PairInput(
            old=SheetSource(str(pair.old_path), 0, scale_text="1 : 100"),
            new=SheetSource(str(pair.new_path), 0, scale_text="1 : 100"),
            pair_id="misaligned",
        ),
        AlignmentInput(matrix=matrix, verdict="good", dpi=DPI),
        config,
    )

    geometry = [
        change
        for change in result.reportable
        if change.primary_stream in {Stream.RASTER, Stream.VECTOR}
    ]
    assert geometry == [], [change.description for change in geometry]


# ── Suppression is always explainable ───────────────────────────────────


def test_everything_suppressed_can_be_retrieved_with_its_reason(matrix):
    for name, (_pair, result) in matrix.items():
        for entry in result.filtered:
            assert entry.filter_name, name
            assert entry.reason, name
            assert entry.change is not None, name


def test_tolerances_are_reported_in_millimetres_never_pixels(matrix):
    _pair, result = matrix["dimension_only"]
    assert result.tolerance is not None
    payload = result.tolerance.as_dict()

    assert payload["position"]["paper_mm"] > 0
    assert payload["position"]["site_mm"] == 25.0
    assert "px" not in json.dumps(payload)


# ── Performance ─────────────────────────────────────────────────────────


def test_one_pair_fits_its_time_budget(matrix):
    for name, (_pair, result) in matrix.items():
        assert result.duration_s < PAIR_BUDGET_S, f"{name} took {result.duration_s:.1f}s"


def test_the_text_stream_is_the_fast_one(matrix):
    """It runs on every pair, so it has to be cheap. Budget: 500 ms."""
    for name, (_pair, result) in matrix.items():
        assert result.timings.get("text", 0.0) < 0.5, name


# ── The golden regression file ──────────────────────────────────────────


def test_the_matrix_matches_its_golden_file(matrix):
    """Any change that lowers precision or raises false positives fails here.

    Regenerate deliberately, never reflexively:

        python -m tests.build_phase5_fixtures --golden
    """
    report = report_for(matrix)
    summary = report.summary()

    if not GOLDEN.exists():  # pragma: no cover - first run only
        pytest.skip(f"No golden file yet. Write one with {GOLDEN.name}.")

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    expected = golden["summary"]

    assert summary["cosmetic_false_positives"] <= expected["cosmetic_false_positives"]
    assert summary["false_positives"] <= expected["false_positives"]
    assert summary["precision"] >= expected["precision"] - 1e-9
    assert summary["recall"] >= expected["recall"] - 1e-9
    assert set(summary["failed_cases"]) <= set(expected["failed_cases"])


def test_the_report_can_be_written_out(matrix, tmp_path: Path):
    report = report_for(matrix)
    csv_path = report.write_csv(tmp_path / "matrix.csv")
    golden_path = report.write_golden(tmp_path / "golden.json")

    assert csv_path.exists()
    assert "precision" in csv_path.read_text(encoding="utf-8")
    assert json.loads(golden_path.read_text(encoding="utf-8"))["summary"]["cases"] == len(
        report.pairs
    )
    assert "precision" in report.table()
