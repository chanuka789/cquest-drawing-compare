"""Task 4.10 tests: the alignment orchestrator and the DoD fixture suite.

The nine ``10_alignment`` cases are built on the fly from
:mod:`tests.fixture_builder` and aligned end to end at 100 dpi (fast renders,
full pipeline): clean_pair, shifted, rotated and rescaled must verify the
recovered geometry, page_rotated and no_grid and sparse_text must reach the
gate, the scanned re-issue must fall through to the image methods, and the
``impossible`` pair is the hard refusal test — a confident wrong alignment
is the worst possible failure, so ``impossible`` MUST return ``failed`` with
every attempt explained. Unit coverage: the manual two/four-point path
(including the documented waiver of the by-rule hold-out refusal), batch
progress + cancellation, and the drawing-scale parser.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from engine.align.anchors import extract_text_anchors
from engine.align.orchestrator import (
    SheetRef,
    _scale_denominator,
    align_batch,
    align_pair,
    apply_manual,
)
from engine.align.transform import decompose
from engine.align.types import AlignConfig, AlignMethod, Verdict
from engine.core.jobs import CancelToken
from engine.extract.raster_renderer import RenderOptions, render_page
from engine.extract.text_extractor import extract_document_text
from tests.fixture_builder import (
    ALIGNMENT_CASES,
    build_alignment_clean,
    build_alignment_shifted,
)

DPI = 100
#: The shifted fixture moves content 40 mm along the sheet's x axis.
SHIFT_PX = 40.0 * DPI / 25.4
#: Title-block scale strings per case: (old, new). The scanned re-issue has
#: no text layer, so its new side cannot name a scale.
CASE_SCALES: dict[str, tuple[str | None, str | None]] = {
    "clean_pair": ("1 : 100", "1 : 100"),
    "shifted": ("1 : 100", "1 : 100"),
    "rescaled": ("1 : 100", "1 : 50"),
    "rotated": ("1 : 100", "1 : 100"),
    "page_rotated": ("1 : 100", "1 : 100"),
    "scanned": ("1 : 100", None),
    "no_grid": ("1 : 100", "1 : 100"),
    "sparse_text": ("1 : 100", "1 : 100"),
    "impossible": ("1 : 100", "1 : 100"),
}


def _only_pdf(folder: Path) -> Path:
    pdfs = sorted(folder.glob("*.pdf"))
    assert pdfs, f"no PDF under {folder}"
    return pdfs[0]


def _result_evidence(result, case_name: str = "") -> str:
    attempts = "; ".join(
        f"{a.method.value}[{a.verdict.value}] {a.reason or '-'}" for a in result.attempts
    )
    return f"{case_name}: {result.note} | attempts: {attempts}"


# ── The DoD fixture suite ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case_name", "builder"),
    [(name, builder) for name, builder in ALIGNMENT_CASES],
    ids=[name for name, _ in ALIGNMENT_CASES],
)
def test_alignment_doD_cases(tmp_path, case_name, builder):
    """Every `10_alignment` case reaches its expected verdict end to end."""
    case_dir = tmp_path / case_name
    builder(case_dir)
    old_pdf = _only_pdf(case_dir / "old")
    new_pdf = _only_pdf(case_dir / "new")
    old_scale, new_scale = CASE_SCALES[case_name]
    result = align_pair(
        SheetRef(abs_path=str(old_pdf), scale_text=old_scale),
        SheetRef(abs_path=str(new_pdf), scale_text=new_scale),
        AlignConfig(dpi=DPI),
    )
    evidence = _result_evidence(result, case_name)

    if case_name == "impossible":
        # Hard requirement: two genuinely different drawings MUST be refused,
        # with every method tried and explained.
        assert result.verdict is Verdict.FAILED, evidence
        assert len(result.attempts) >= 3, evidence
        methods = {attempt.method for attempt in result.attempts}
        assert len(methods) >= 3, evidence
        assert all(attempt.reason for attempt in result.attempts), evidence
        assert all(
            attempt.verdict in {Verdict.POOR, Verdict.FAILED} for attempt in result.attempts
        ), evidence
        return

    assert result.verdict in {Verdict.GOOD, Verdict.EXCELLENT}, evidence
    assert result.matrix is not None
    parts = decompose(result.matrix)
    scale = math.sqrt(abs(parts.determinant))

    if case_name == "clean_pair":
        assert result.method is AlignMethod.TEXT_ANCHORS, evidence
    elif case_name == "shifted":
        assert abs(parts.tx - SHIFT_PX) < 8.0, evidence
        assert abs(scale - 1.0) < 0.02, evidence
    elif case_name == "rotated":
        assert abs(abs(parts.rotation_deg) - 90.0) <= 1.5, evidence
    elif case_name == "rescaled":
        assert abs(scale - 2.0) < 0.03, evidence
    elif case_name == "scanned":
        # The scanned re-issue has no text layer, so only the image methods
        # can align it. If this ever measures poor instead of good, the
        # numbers in `evidence` are the tuning data — this assertion is kept
        # at good/excellent deliberately.
        assert result.method in {
            AlignMethod.PHASE_CORRELATION,
            AlignMethod.FEATURES,
        }, evidence
    # page_rotated, no_grid and sparse_text only need the verdict: upright
    # renders / a grid-free sheet / a nearly text-free sheet must all align.


# ── Refusal honesty (unit view of the same hard requirement) ─────────────


def test_impossible_refusal_records_every_method_with_reasons(tmp_path):
    """impossible: failed, and the attempts tell the whole story."""
    from tests.fixture_builder import build_alignment_impossible

    case_dir = tmp_path / "impossible"
    build_alignment_impossible(case_dir)
    result = align_pair(
        SheetRef(abs_path=str(_only_pdf(case_dir / "old")), scale_text="1 : 100"),
        SheetRef(abs_path=str(_only_pdf(case_dir / "new")), scale_text="1 : 100"),
        AlignConfig(dpi=DPI),
    )
    evidence = _result_evidence(result, "impossible")
    assert result.verdict is Verdict.FAILED, evidence
    assert len(result.attempts) >= 3, evidence
    # Text anchors found nothing shared; the image methods each explain why
    # their fit did not pass the gate.
    attempted = {attempt.method for attempt in result.attempts}
    assert AlignMethod.TEXT_ANCHORS in attempted, evidence
    assert AlignMethod.PHASE_CORRELATION in attempted, evidence
    assert all(attempt.reason for attempt in result.attempts), evidence
    assert all(attempt.detail for attempt in result.attempts), evidence


# ── Manual alignment ─────────────────────────────────────────────────────


@pytest.mark.parametrize("point_count", [2, 4])
def test_apply_manual_recovers_shifted_content(tmp_path, point_count):
    """Two (or four) clicked pairs recover the 40 mm shift with a good verdict.

    Two/four-point fits cannot be cross-validated — the gate's hold-out rule
    needs at least five — so a gate verdict of ``failed`` is waived to
    ``good`` only through the documented manual exception (ink overlap and
    sanity must pass). This test pins that the waiver fires and the geometry
    is exact. The clicked labels are chosen dynamically as the corner-most
    anchors both sheets provably share (the title-block zone detector eats
    some row-0/column-5 labels on these fixture sheets, so hard-coded names
    would be brittle).
    """
    root = tmp_path / "manual"
    build_alignment_shifted(root)
    old_pdf = _only_pdf(root / "old")
    new_pdf = _only_pdf(root / "new")
    options = RenderOptions(dpi=DPI, colour=False)
    old_render = render_page(str(old_pdf), 0, options)
    new_render = render_page(str(new_pdf), 0, options)
    old_page = extract_document_text(str(old_pdf), pages=[0])[0]
    new_page = extract_document_text(str(new_pdf), pages=[0])[0]
    old_anchors = {a.text: a for a in extract_text_anchors(old_page, DPI)}
    new_anchors = {a.text: a for a in extract_text_anchors(new_page, DPI)}

    shared = sorted(set(old_anchors) & set(new_anchors))
    assert len(shared) >= 4, "not enough shared anchors on the shifted pair"
    if point_count == 2:
        chosen = max(
            ((a, b) for a in shared for b in shared if a < b),
            key=lambda pair: math.hypot(
                old_anchors[pair[0]].x - old_anchors[pair[1]].x,
                old_anchors[pair[0]].y - old_anchors[pair[1]].y,
            ),
        )
    else:
        chosen = sorted(
            {
                min(shared, key=lambda name: old_anchors[name].x),
                max(shared, key=lambda name: old_anchors[name].x),
                min(shared, key=lambda name: old_anchors[name].y),
                max(shared, key=lambda name: old_anchors[name].y),
            }
        )

    old_points: list[tuple[float, float]] = []
    new_points: list[tuple[float, float]] = []
    for name in chosen:
        assert name in old_anchors and name in new_anchors, name
        old_points.append((old_anchors[name].x, old_anchors[name].y))
        new_points.append((new_anchors[name].x, new_anchors[name].y))

    result = apply_manual(
        old_points,
        new_points,
        AlignConfig(dpi=DPI),
        old_img=old_render.grayscale,
        new_img=new_render.grayscale,
        px_per_mm=old_render.px_per_mm,
    )
    evidence = _result_evidence(result, f"manual-{point_count}")
    assert result.verdict is Verdict.GOOD, evidence
    assert result.method is AlignMethod.MANUAL, evidence
    assert len(result.attempts) == 1, evidence
    assert result.assessment is not None, evidence
    parts = decompose(result.matrix)
    assert abs(parts.tx - SHIFT_PX) < 8.0, evidence
    assert abs(math.sqrt(abs(parts.determinant)) - 1.0) < 0.02, evidence


# ── Batch ────────────────────────────────────────────────────────────────


def test_align_batch_progress_increments_and_cancel_stops(tmp_path):
    """Batch over three pairs: progress increments; cancel stops after one."""
    root = tmp_path / "batch"
    build_alignment_clean(root)
    old_pdf = _only_pdf(root / "old")
    new_pdf = _only_pdf(root / "new")
    pair = (
        SheetRef(abs_path=str(old_pdf), scale_text="1 : 100"),
        SheetRef(abs_path=str(new_pdf), scale_text="1 : 100"),
    )
    config = AlignConfig(dpi=DPI)

    token = CancelToken()
    events: list[tuple[int, int, str]] = []

    def cancelling_progress(completed: int, total: int, label: str) -> None:
        events.append((completed, total, label))
        if completed >= 1:
            token.cancel()

    results = align_batch([pair] * 3, config, progress=cancelling_progress, cancel=token)
    assert token.cancelled
    assert len(results) == 1
    assert results[0].verdict in {Verdict.GOOD, Verdict.EXCELLENT}
    assert events[0][:2] == (0, 3)
    assert events[-1][:2] == (1, 3)
    completed = [completed for completed, _, _ in events]
    assert completed == sorted(completed)

    # A fresh, never-cancelled run finishes all three pairs.
    full_events: list[tuple[int, int, str]] = []
    results = align_batch(
        [pair] * 3,
        config,
        progress=lambda completed, total, label: full_events.append((completed, total, label)),
        cancel=CancelToken(),
    )
    assert len(results) == 3
    assert all(r.verdict in {Verdict.GOOD, Verdict.EXCELLENT} for r in results)
    assert full_events[0][:2] == (0, 3)
    assert full_events[-1][:2] == (3, 3)
    assert len(full_events) == 6


# ── Scale parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 : 100", 100),
        ("1:100", 100),
        ("1/100", 100),
        ("SCALE 1:50", 50),
        ("1 : 200@A1", 200),
        ("1 : 1", 1),
    ],
)
def test_scale_denominator_parses(text, expected):
    assert _scale_denominator(text) == expected


@pytest.mark.parametrize("text", [None, "", "NTS", "not to scale", "1.5:1"])
def test_scale_denominator_absent(text):
    assert _scale_denominator(text) is None
