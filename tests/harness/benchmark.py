"""Ground-truth alignment benchmark (Phase 4, Task 4.0).

Runs an alignment function across a matrix of synthetic pairs whose ground
truth is known exactly, and reports, per case: the transform parameters, the
computed transform, the harness-measured error in px, the method, the
verdict, and the time taken. It also writes a CSV when asked.

The one number that matters is the **dangerous failure count**: cases where
the verdict claimed ``excellent``/``good``/``poor`` while the true error
exceeded 2 px. That count must be zero (Phase 4 plan, section B6 and Task
4.15). ``error_px`` in every row is the harness measurement used for that
rule; ``true_rms_px`` is the residual the alignment result itself reported
(``rms_residual_px``/``rms_px``/``residual_px``/``rms``), when it reports
one, so a claimed residual far below ``error_px`` exposes a quality gate
that lies.

``align_fn`` is either a callable returning an object with ``.matrix``
(2x3 or 3x3, old -> new), ``.verdict`` (excellent/good/poor/failed) and
``.method`` attributes, or a callable returning a ``(matrix, verdict)``
tuple. Both forms are duck-typed so engine alignment results can be plugged
in unchanged.
"""

from __future__ import annotations

import csv
import math
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from tests.fixture_builder import SheetSpec, build_pdf
from tests.harness.synthetic import (
    DegradationSpec,
    TransformSpec,
    generate_pair,
    similarity_parameters,
)

#: Verdicts that claim the alignment is usable; a dangerous failure is one of
#: these paired with a true error over the 2 px RMS gate.
_CLAIMED_VERDICTS = {"excellent", "good", "poor"}
_ALL_VERDICTS = _CLAIMED_VERDICTS | {"failed"}
_SUCCESS_VERDICTS = {"excellent", "good"}

#: How the CSV columns are ordered.
CSV_COLUMNS = [
    "case",
    "translation_mm_x",
    "translation_mm_y",
    "rotation_deg",
    "scale",
    "degradation",
    "true_rms_px",
    "computed_scale",
    "computed_rotation",
    "method",
    "verdict",
    "error_px",
    "time_s",
    "dangerous",
]

#: The three severities every benchmark matrix is crossed with.
NONE_DEGRADATION = DegradationSpec()
MILD_DEGRADATION = DegradationSpec(blur_sigma=0.8, noise_sigma=3.0, line_weight_change=1)
STRONG_DEGRADATION = DegradationSpec(blur_sigma=1.5, noise_sigma=8.0, jpeg_quality=60)


@dataclass(frozen=True, slots=True)
class BenchCase:
    """One benchmark case: a transform and its degradation, named for the CSV."""

    name: str
    transform: TransformSpec
    degradation: DegradationSpec


def describe_degradation(spec: DegradationSpec) -> str:
    """A short human-readable label for a degradation spec."""
    parts: list[str] = []
    if spec.line_weight_change:
        parts.append(f"line{spec.line_weight_change:+d}")
    if spec.noise_sigma:
        parts.append(f"noise{spec.noise_sigma:g}")
    if spec.jpeg_quality is not None:
        parts.append(f"jpeg{spec.jpeg_quality}")
    if spec.skew_deg:
        parts.append(f"skew{spec.skew_deg:g}")
    if spec.blur_sigma:
        parts.append(f"blur{spec.blur_sigma:g}")
    if spec.content_change != (None, None):
        rect, shape = spec.content_change
        if rect is not None:
            parts.append(f"erase({rect[0]},{rect[1]},{rect[2]},{rect[3]})")
        if shape is not None:
            parts.append(shape)
    return "+".join(parts) if parts else "none"


def default_cases(*, full: bool = False, page_w_mm: float = 841.0) -> list[BenchCase]:
    """The benchmark case matrix (A1 page width in mm by default).

    Translations run from 0 to half the page width, rotations over
    ``[0, 1.5, 45, 90, 180]`` degrees, scales over
    ``[0.5, 0.707, 1.0, 1.414, 2.0]``, each crossed with the three
    degradations. ``full=False`` (the default) returns a small smoke subset
    with no degradation so a smoke test stays fast; the full 225-case matrix
    is available with ``full=True``.
    """
    if full:
        fractions = (0.0, 0.25, 0.5)
        rotations = (0.0, 1.5, 45.0, 90.0, 180.0)
        scales = (0.5, 0.707, 1.0, 1.414, 2.0)
        degradations = (NONE_DEGRADATION, MILD_DEGRADATION, STRONG_DEGRADATION)
    else:
        fractions = (0.0, 0.25)
        rotations = (0.0, 1.5)
        scales = (1.0,)
        degradations = (NONE_DEGRADATION,)
    cases: list[BenchCase] = []
    for fraction in fractions:
        shift = fraction * page_w_mm
        for rotation in rotations:
            for scale in scales:
                for degradation in degradations:
                    name = (
                        f"tx{shift:g}_r{rotation:g}_s{scale:g}_{describe_degradation(degradation)}"
                    )
                    cases.append(
                        BenchCase(
                            name=name,
                            transform=TransformSpec(
                                translation_mm=(shift, shift),
                                rotation_deg=rotation,
                                scale=scale,
                            ),
                            degradation=degradation,
                        )
                    )
    return cases


def _normalise_matrix(matrix: Any) -> NDArray[np.float64]:
    arr = np.asarray(matrix, dtype=float)
    if arr.shape == (3, 3):
        return arr
    if arr.shape == (2, 3):
        return np.vstack([arr, np.array([0.0, 0.0, 1.0])])
    raise ValueError(f"align result .matrix must be 2x3 or 3x3, got shape {arr.shape}")


def _reported_residual(result: Any) -> float:
    """The residual an alignment result claims, if it carries one."""
    for name in ("rms_residual_px", "rms_px", "residual_px", "rms"):
        value = getattr(result, name, None)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return float("nan")


def _normalise_result(result: Any, default_method: str = "unknown") -> dict[str, Any]:
    """Turn any supported align_fn outcome into matrix/verdict/method/rms."""
    if isinstance(result, (tuple, list)):
        if len(result) == 2:
            matrix, verdict = result
            method = default_method
            residual = float("nan")
        elif len(result) == 3:
            matrix, verdict, method = result
            residual = float("nan")
        else:
            raise ValueError(
                f"align_fn tuple results need (matrix, verdict), got {len(result)} items"
            )
    else:
        matrix = getattr(result, "matrix", None)
        verdict = getattr(result, "verdict", None)
        method = str(getattr(result, "method", None) or default_method)
        residual = _reported_residual(result)
    verdict_str = str(verdict).strip().lower()
    if verdict_str not in _ALL_VERDICTS:
        raise ValueError(f"align verdict must be one of {sorted(_ALL_VERDICTS)!r}, got {verdict!r}")
    return {
        "matrix": _normalise_matrix(matrix),
        "verdict": verdict_str,
        "method": method,
        "claimed_rms_px": residual,
    }


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


@dataclass(slots=True)
class BenchmarkReport:
    """Every case result plus the failures that matter."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: Verdicts in {excellent, good, poor} whose true error exceeded 2 px.
    dangerous_failures: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        """Print and return a per-method summary table.

        Success rate counts verdicts of excellent/good; mean and p95 error
        are over the harness-measured ``error_px``; time is per case.
        """
        methods: list[str] = []
        for row in self.rows:
            method = str(row["method"])
            if method not in methods:
                methods.append(method)
        lines = [
            f"Benchmark: {len(self.rows)} cases, "
            f"{len(self.dangerous_failures)} dangerous failures "
            "(verdict excellent/good/poor with true error > 2 px)",
            f"{'method':<16}{'cases':>6}{'success':>9}{'mean_err':>10}{'p95_err':>10}"
            f"{'mean_time_s':>12}",
        ]
        for method in [*methods, "(all)"]:
            group = (
                self.rows if method == "(all)" else [r for r in self.rows if r["method"] == method]
            )
            if not group:
                continue
            cases = len(group)
            successes = sum(1 for r in group if r["verdict"] in _SUCCESS_VERDICTS)
            errors = [
                float(r["error_px"])
                for r in group
                if isinstance(r["error_px"], float) and not math.isnan(r["error_px"])
            ]
            times = [float(r["time_s"]) for r in group]
            mean = float(np.mean(errors)) if errors else float("nan")
            p95 = float(np.percentile(errors, 95)) if errors else float("nan")
            mean_time = float(np.mean(times)) if times else float("nan")
            success_pct = 100.0 * successes / cases
            line = (
                f"{method:<16}{cases:>6}{success_pct:>8.1f}%{mean:>10.3f}{p95:>10.3f}"
                f"{mean_time:>12.4f}"
            )
            lines.append(line)
        text = "\n".join(lines)
        print(text)  # noqa: T201 - the benchmark report prints by design
        return text


def run_benchmark(
    align_fn: Callable[[NDArray[np.uint8], NDArray[np.uint8]], Any],
    cases: list[BenchCase] | None = None,
    out_csv: str | Path | None = None,
    *,
    source_pdf: str | Path | None = None,
    page: int = 0,
    dpi: int = 100,
) -> BenchmarkReport:
    """Run ``align_fn`` over ``cases`` and return a :class:`BenchmarkReport`.

    With no ``cases`` the smoke subset of :func:`default_cases` is used, and
    with no ``source_pdf`` a standard A1 fixture sheet is built in a
    temporary directory and removed afterwards. Per case, ``align_fn``
    receives the (old, new) grayscale images; the harness then measures the
    true RMS error of the returned matrix against the ground truth.
    """
    if cases is None:
        cases = default_cases()
    temporary_dir: tempfile.TemporaryDirectory[str] | None = None
    if source_pdf is None:
        temporary_dir = tempfile.TemporaryDirectory(prefix="cqdc-bench-")
        source_pdf = Path(temporary_dir.name) / "source.pdf"
        build_pdf(source_pdf, [SheetSpec()])
    report = BenchmarkReport()
    try:
        for case in cases:
            pair = generate_pair(
                source_pdf,
                page=page,
                transform=case.transform,
                degradation=case.degradation,
                dpi=dpi,
            )
            started = time.perf_counter()
            try:
                outcome = _normalise_result(align_fn(pair.old_image, pair.new_image))
            except Exception as exc:
                outcome = {
                    "matrix": None,
                    "verdict": "failed",
                    "method": f"raised-{type(exc).__name__}",
                    "claimed_rms_px": float("nan"),
                }
            elapsed = time.perf_counter() - started
            matrix = outcome["matrix"]
            if matrix is not None:
                error_px = pair.error_of(matrix)
                _, _, computed_rotation, computed_scale = similarity_parameters(matrix)
            else:
                error_px = float("nan")
                computed_rotation = float("nan")
                computed_scale = float("nan")
            shift = case.transform.translation_mm
            row: dict[str, Any] = {
                "case": case.name,
                "translation_mm_x": shift[0],
                "translation_mm_y": shift[1],
                "rotation_deg": case.transform.rotation_deg,
                "scale": case.transform.scale,
                "degradation": describe_degradation(case.degradation),
                "true_rms_px": outcome["claimed_rms_px"],
                "computed_scale": computed_scale,
                "computed_rotation": computed_rotation,
                "method": outcome["method"],
                "verdict": outcome["verdict"],
                "error_px": error_px,
                "time_s": elapsed,
                "dangerous": outcome["verdict"] in _CLAIMED_VERDICTS
                and isinstance(error_px, float)
                and not math.isnan(error_px)
                and error_px > 2.0,
            }
            report.rows.append(row)
            if row["dangerous"]:
                report.dangerous_failures.append(row)
    finally:
        if temporary_dir is not None:
            temporary_dir.cleanup()
    if out_csv is not None:
        target = Path(out_csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for row in report.rows:
                writer.writerow([_csv_value(row[column]) for column in CSV_COLUMNS])
    return report
