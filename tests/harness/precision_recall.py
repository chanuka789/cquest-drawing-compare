"""Task 5.0 (measurement) — precision, recall, and the false positive count.

Without this, filters get tuned by feel: someone looks at a report, thinks it
seems noisy, changes a threshold, and looks again. That process converges on
whichever sheet was open at the time.

So the injection harness builds pairs whose changes are known exactly, the
comparison engine runs on them, and the reported changes are matched against
the truth by bounding box overlap. Three numbers come out:

* **precision** — of what was reported, how much was real,
* **recall** — of what was real, how much was found,
* **false positives** — and on a cosmetic-only pair that number must be zero.

The last one is the headline metric of the whole phase. Precision and recall
can be traded against each other and reasonable people will disagree about
where the line sits. "A pair where nothing changed reports nothing" is not a
trade-off, it is the gate.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine.align.anchors import page_points_to_image_px
from engine.compare.orchestrator import (
    AlignmentInput,
    CompareConfig,
    ComparisonResult,
    PairInput,
    SheetSource,
    compare_pair,
)
from engine.compare.types import Bbox
from engine.utils.pdf_runtime import open_document
from tests.harness.inject import ChangeSpec, ExpectedChange, InjectedPair, inject_changes

#: Reported and expected boxes overlapping by more than this are the same
#: change. Deliberately loose: a change region found by the raster stream is
#: a different shape from the object that moved, and demanding a tight match
#: would score a correct finding as both a miss and a false positive.
MATCH_IOU = 0.3
#: A small reported box sitting inside a large expected one also counts.
CONTAINMENT = 0.6


@dataclass(slots=True)
class PairMetrics:
    """How one injected pair scored."""

    name: str
    expected: int = 0
    reported: int = 0
    #: Reported records that hit at least one injected change. Drives precision.
    matched: int = 0
    #: Injected changes accounted for by some record. Drives recall.
    covered: int = 0
    false_positives: int = 0
    missed: int = 0
    cosmetic_only: bool = False
    cosmetic_reported: int = 0
    duration_s: float = 0.0
    missed_labels: list[str] = field(default_factory=list)
    false_positive_descriptions: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """1.0 when nothing was reported and nothing was expected."""
        if self.reported == 0:
            return 1.0
        return self.matched / self.reported

    @property
    def recall(self) -> float:
        """Of what was injected, how much the engine accounted for.

        Counted in *injected changes covered*, not in records reported: one
        record that correctly covers four renumbered tags has found all four.
        """
        if self.expected == 0:
            return 1.0
        return self.covered / self.expected

    @property
    def passed(self) -> bool:
        """A cosmetic-only pair passes only with zero reported changes."""
        if self.cosmetic_only:
            return self.reported == 0
        return self.missed == 0 and self.false_positives == 0

    def as_row(self) -> dict[str, Any]:
        return {
            "case": self.name,
            "cosmetic_only": self.cosmetic_only,
            "expected": self.expected,
            "reported": self.reported,
            "matched": self.matched,
            "covered": self.covered,
            "false_positives": self.false_positives,
            "missed": self.missed,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "duration_s": round(self.duration_s, 2),
            "passed": self.passed,
        }


@dataclass(slots=True)
class MatrixReport:
    """The whole injection matrix, scored."""

    pairs: list[PairMetrics] = field(default_factory=list)

    @property
    def precision(self) -> float:
        reported = sum(metric.reported for metric in self.pairs)
        matched = sum(metric.matched for metric in self.pairs)
        return matched / reported if reported else 1.0

    @property
    def recall(self) -> float:
        expected = sum(metric.expected for metric in self.pairs)
        covered = sum(metric.covered for metric in self.pairs)
        return covered / expected if expected else 1.0

    @property
    def cosmetic_false_positives(self) -> int:
        """The headline metric. It must be zero."""
        return sum(metric.reported for metric in self.pairs if metric.cosmetic_only)

    @property
    def total_false_positives(self) -> int:
        return sum(metric.false_positives for metric in self.pairs)

    def summary(self) -> dict[str, Any]:
        return {
            "cases": len(self.pairs),
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "false_positives": self.total_false_positives,
            "cosmetic_false_positives": self.cosmetic_false_positives,
            "failed_cases": sorted(metric.name for metric in self.pairs if not metric.passed),
        }

    def table(self) -> str:
        """A fixed-width table, for a terminal and for the golden file diff."""
        header = (
            f"{'case':24} {'cosm':5} {'exp':>4} {'rep':>4} {'hit':>4} "
            f"{'fp':>4} {'miss':>5} {'prec':>6} {'rec':>6}  {'time':>6}"
        )
        lines = [header, "-" * len(header)]
        for metric in sorted(self.pairs, key=lambda item: item.name):
            lines.append(
                f"{metric.name:24} {'yes' if metric.cosmetic_only else 'no':5} "
                f"{metric.expected:4d} {metric.reported:4d} {metric.matched:4d} "
                f"{metric.false_positives:4d} {metric.missed:5d} "
                f"{metric.precision:6.2f} {metric.recall:6.2f}  {metric.duration_s:5.2f}s"
            )
        summary = self.summary()
        lines.append("-" * len(header))
        lines.append(
            f"precision {summary['precision']:.3f}  recall {summary['recall']:.3f}  "
            f"false positives {summary['false_positives']}  "
            f"cosmetic false positives {summary['cosmetic_false_positives']}"
        )
        return "\n".join(lines)

    def write_csv(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [metric.as_row() for metric in sorted(self.pairs, key=lambda item: item.name)]
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["case"])
            writer.writeheader()
            writer.writerows(rows)
        return target

    def write_golden(self, path: str | Path) -> Path:
        """The regression file. A drop in precision fails the suite."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": self.summary(),
            "cases": {
                metric.name: {
                    "expected": metric.expected,
                    "reported": metric.reported,
                    "matched": metric.matched,
                    "covered": metric.covered,
                    "false_positives": metric.false_positives,
                    "cosmetic_only": metric.cosmetic_only,
                }
                for metric in self.pairs
            },
        }
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return target


# ── Matching reported changes against the truth ─────────────────────────


def expected_to_pixels(expected: ExpectedChange, pdf_path: str | Path, dpi: int) -> Bbox:
    """An injected change's box in comparison pixel space.

    The harness records truth in PDF user-space points on the new page; the
    engine reports in image pixels. The conversion goes through the same
    helper the alignment and text streams use, so a media box that does not
    start at the origin is handled once and identically everywhere.
    """
    with open_document(str(pdf_path)) as document:
        page = document[0]
        box = page.get_mediabox()
        x0, y0, x1, y1 = (float(value) for value in box)

    left, top = page_points_to_image_px(
        expected.bbox_pt[0], expected.bbox_pt[3], x0, y0, x1 - x0, y1 - y0, dpi
    )
    right, bottom = page_points_to_image_px(
        expected.bbox_pt[2], expected.bbox_pt[1], x0, y0, x1 - x0, y1 - y0, dpi
    )
    return Bbox.from_corners(left, top, right, bottom)


def _matches(reported: Bbox, truth: Bbox) -> bool:
    if reported.iou(truth) >= MATCH_IOU:
        return True
    smaller, larger = sorted((reported, truth), key=lambda box: box.area)
    if smaller.area <= 0:
        return False
    return smaller.intersection_area(larger) / smaller.area >= CONTAINMENT


def score_pair(pair: InjectedPair, result: ComparisonResult, dpi: int) -> PairMetrics:
    """Match one pair's reported changes against its known truth."""
    metrics = PairMetrics(
        name=pair.name,
        expected=len(pair.expected),
        cosmetic_only=pair.is_cosmetic_only,
        duration_s=result.duration_s,
    )

    reported = result.reportable
    metrics.reported = len(reported)
    metrics.cosmetic_reported = len(result.cosmetic)

    truths = [expected_to_pixels(item, pair.new_path, dpi) for item in pair.expected]
    claimed: set[int] = set()

    for change in reported:
        # One reported record may legitimately cover several injected
        # changes: that is the whole point of collapsing four renumbered
        # tags into one record, or eight deleted lines into one region.
        # Scoring it as one hit and three misses would penalise the engine
        # for doing exactly what the plan asks.
        hits = [
            index
            for index, truth in enumerate(truths)
            if index not in claimed and _matches(change.bbox, truth)
        ]
        if not hits:
            metrics.false_positives += 1
            metrics.false_positive_descriptions.append(change.description)
            continue
        claimed.update(hits)
        metrics.matched += 1

    metrics.covered = len(claimed)
    metrics.missed = len(truths) - len(claimed)
    metrics.missed_labels = [
        pair.expected[index].label for index in range(len(truths)) if index not in claimed
    ]
    return metrics


# ── Running the matrix ──────────────────────────────────────────────────


def run_case(
    source_pdf: str | Path,
    spec: ChangeSpec,
    out_dir: str | Path,
    *,
    config: CompareConfig | None = None,
) -> tuple[InjectedPair, ComparisonResult, PairMetrics]:
    """Inject one case, compare it, and score it."""
    config = config or CompareConfig()
    pair = inject_changes(source_pdf, 0, spec, out_dir=out_dir)

    comparison = compare_pair(
        PairInput(
            old=SheetSource(str(pair.old_path), pair.page_index, scale_text="1 : 100"),
            new=SheetSource(str(pair.new_path), pair.page_index, scale_text="1 : 100"),
            pair_id=pair.name,
            label=pair.name,
        ),
        # The injected pair is built from one source, so the sheets are
        # already in register: identity is the true transform, and any
        # misalignment the engine reports would be its own invention.
        AlignmentInput(matrix=np.eye(3), verdict="good", dpi=config.dpi),
        config,
    )
    return pair, comparison, score_pair(pair, comparison, config.dpi)


def run_matrix(
    source_pdf: str | Path,
    specs: list[ChangeSpec],
    out_dir: str | Path,
    *,
    config: CompareConfig | None = None,
) -> MatrixReport:
    """Run every case and collect the scores."""
    config = config or CompareConfig()
    report = MatrixReport()
    for spec in specs:
        _pair, _result, metrics = run_case(source_pdf, spec, out_dir, config=config)
        report.pairs.append(metrics)
    return report
