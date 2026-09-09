"""Phase 4.15: ground-truth benchmark with a committed golden file.

The benchmark measures the IMAGE-method pipeline (phase correlation -> SIFT
features -> sheet border, each assessed by the quality gate) against synthetic
pairs whose true transform is known exactly. The number that matters is the
**dangerous-failure count**: cases whose verdict was ``good`` or better while
the true error exceeded 2 px. That number must be zero.

The normal suite runs a compact case matrix and compares the outcome with the
committed golden file (any future change that raises mean error by more than
25 % or produces a dangerous failure fails the suite). The full matrix from
the plan runs only under ``pytest -m benchmark``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from engine.align.quality import assess
from engine.align.types import AlignConfig, Verdict
from tests.fixture_builder import write_drawing_pdf
from tests.harness.benchmark import default_cases, run_benchmark

GOLDEN_PATH = Path(__file__).with_name("benchmark_golden.json")
DPI = 100
GOLDEN_TOLERANCE = 0.25  # 25 % drift on the mean error fails the guard


class _ImageMethodResult:
    """Duck-typed result the harness accepts (matrix/verdict/method)."""

    def __init__(self, matrix: np.ndarray, verdict: str, method: str) -> None:
        self.matrix = matrix
        self.verdict = verdict
        self.method = method


def _align_images(old_img: np.ndarray, new_img: np.ndarray) -> _ImageMethodResult:
    """The image-only cascade: phase correlation, features, sheet border.

    Every attempt is polished with ECC (the same refinement the orchestrator
    applies) and judged by the quality gate; the first ``good`` or better
    wins, otherwise the best attempt is returned (or ``failed`` when none
    passed).
    """
    from engine.align.image_align import (
        align_features,
        align_phase_correlation,
        align_sheet_border,
    )
    from engine.align.refine import refine_ecc

    config = AlignConfig(dpi=DPI)
    attempts: list[tuple[Verdict, np.ndarray, str]] = []
    for name, runner in (
        ("phase_correlation", lambda: align_phase_correlation(old_img, new_img, config=config)),
        ("features", lambda: align_features(old_img, new_img, config=config)),
        ("sheet_border", lambda: align_sheet_border(old_img, new_img)),
    ):
        try:
            result = runner()
        except Exception:
            continue
        matrix = np.asarray(getattr(result, "matrix", np.eye(3)))
        if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
            continue
        refined = refine_ecc(old_img, new_img, matrix, config=config)
        if refined.applied:
            matrix = refined.matrix
        assessment = assess(
            result if not refined.applied else _replacement_result(matrix, name),
            [],
            old_img,
            new_img,
            config=config,
            px_per_mm=DPI / 25.4,
        )
        attempts.append((assessment.verdict, matrix, name))
        if assessment.verdict in {Verdict.GOOD, Verdict.EXCELLENT}:
            return _ImageMethodResult(matrix, str(assessment.verdict), name)

    if not attempts:
        return _ImageMethodResult(np.eye(3), "failed", "none")
    rank = {Verdict.EXCELLENT: 0, Verdict.GOOD: 1, Verdict.POOR: 2, Verdict.FAILED: 3}
    attempts.sort(key=lambda item: rank[item[0]])
    verdict, matrix, name = attempts[0]
    return _ImageMethodResult(matrix, str(verdict), name)


def _replacement_result(matrix: np.ndarray, method: str):
    """A TransformResult carrying a refined matrix (ECC outcome)."""
    from engine.align.image_align import _result_from_matrix
    from engine.align.types import AlignMethod

    return _result_from_matrix(matrix, AlignMethod(method), 1.0, "refined by ECC")


def _run(matrix: list[object] | None = None) -> object:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cqdc-bench-") as folder:
        source = write_drawing_pdf(
            Path(folder) / "source.pdf",
            drawing_no="B-100",
            revision="C",
            scale="1 : 100",
        )
        return run_benchmark(
            _align_images,
            cases=matrix or default_cases(full=False),
            source_pdf=source,
            page=0,
            dpi=DPI,
        )


def _golden_summary(report: object) -> dict[str, object]:
    dangerous = len(getattr(report, "dangerous_failures", []))
    rows = getattr(report, "rows", [])
    errors = [float(row.get("error_px", float("nan"))) for row in rows]
    errors = [value for value in errors if np.isfinite(value)]
    per_method: dict[str, list[float]] = {}
    for row in rows:
        per_method.setdefault(str(row.get("method", "?")), []).append(
            float(row.get("error_px", float("nan")))
        )
    return {
        "cases": len(rows),
        "dangerous": dangerous,
        "mean_error_px": float(np.mean(errors)) if errors else None,
        "p95_error_px": float(np.percentile(errors, 95)) if errors else None,
        "per_method_mean": {
            name: float(np.mean([value for value in values if np.isfinite(value)]))
            for name, values in per_method.items()
            if any(np.isfinite(value) for value in values)
        },
    }


@pytest.mark.benchmark
def test_full_benchmark_matrix_has_zero_dangerous_failures():
    """The plan's full matrix; run explicitly with `pytest -m benchmark`.

    Unmatched (failed-verdict) cases may carry large raw error - refusing
    honestly is the correct outcome for them. What must never happen is a
    ``good``-or-better verdict whose true error exceeds 2 px.
    """
    report = _run(default_cases(full=True))
    dangerous = getattr(report, "dangerous_failures", [])
    assert len(dangerous) == 0, dangerous[:5]
    summary = _golden_summary(report)
    assert summary["mean_error_px"] is not None


def test_golden_benchmark_guard():
    """Compact matrix every suite run: zero dangerous, within 25 % of golden."""
    report = _run()
    summary = _golden_summary(report)
    assert summary["dangerous"] == 0, "dangerous failure: verdict good+ with true error > 2 px"
    assert summary["mean_error_px"] is not None

    if not GOLDEN_PATH.is_file() or os.environ.get("REWRITE_GOLDEN"):
        GOLDEN_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    mean = float(golden["mean_error_px"])
    assert summary["mean_error_px"] <= mean * (1.0 + GOLDEN_TOLERANCE), (
        f"mean error {summary['mean_error_px']:.3f} px exceeds golden {mean:.3f} px "
        f"by more than {GOLDEN_TOLERANCE:.0%}"
    )
