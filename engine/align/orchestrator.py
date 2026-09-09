"""Task 4.10: the alignment orchestrator — the strategy cascade.

This module turns two loaded sheets (a :class:`SheetRef` each) into one
decision: an :class:`AlignmentResult` carrying the winning transform, its
:class:`QualityAssessment` and every attempt that was made, recorded even
when it failed, so the review screen can explain what was tried and why each
attempt did not win.

The cascade (plan B4, task 4.10):

1. **Grid bubbles** — circles with text-layer labels, matched by label.
   Six or more matched labels are the strongest anchors a construction
   drawing carries, so they go first.
2. **Unique text anchors** — text strings that appear exactly once on each
   sheet. Best for CAD PDFs; needs three or more.
3. **Phase correlation** — global, needs no features, best for scanned
   sheets. The quality gate judges it (and every method below) by its
   post-warp ink overlap, because it produces no point correspondences.
4. **Features (SIFT)** — the fallback the plan is honest about being a
   fallback.
5. **Sheet border corners** — aligns the sheet frame, never the content;
   accepted only on ``good`` or better, and only after every content method
   failed.

Safety rules implemented here, all from plan B3/B6:

* Every method must pass the quality gate (``good`` or ``excellent``) before
  the pair returns. A ``poor`` fit needs a human eye and never ends the
  cascade; a confident wrong transform is the worst possible failure, so the
  final answer for an unaligned pair is ``failed`` with the best attempt
  attached, never a silent downgrade.
* **Affine is scanned-only.** A similarity attempt that fails may be
  re-fitted with an affine model *only* when one of the two sheets was
  detected as scanned (rasterised). CAD sheets never escalate.
  **Homography is never invoked by ``align_pair``** even when
  ``AlignConfig.allow_homography`` is set — it remains an explicit user
  opt-in for callers outside this module; ``transform.fit_transform``
  refuses to fit it anyway.
* **The metadata scale prior is a diagnostic, not a gate.** The expected
  content scale (title-block scale denominators times sheet-size ratio) is
  computed when both sides name a scale, but it is only *recorded* into the
  attempt detail when the fitted scale is off — it never rejects an attempt
  by itself. Title-block scales are frequently wrong or absent in the real
  world, and the other six gate metrics already catch genuinely insane
  fits. (The gate's transform-sanity metric applies broad 0.2x-5x scale
  bounds; passing those is the only scale refusal that exists.)
* **ECC refinement** polishes every accepted fit on full-resolution
  grayscale images, but only when both renders share dimensions; otherwise
  it is skipped (``RefineResult.applied`` would be False anyway). ECC is
  advisory: its chamfer acceptance rule can be misled when content partially
  leaves the canvas, so a refinement the quality gate refuses is re-checked
  against the coarse fit and dropped when the coarse fit passes the gate —
  the gate is the arbiter, never ECC's internal metric.
* **A time budget** (``AlignConfig.pair_timeout_s``, default 30 s) bounds
  the whole cascade. When it runs out the pair returns ``failed`` with the
  best attempt so far attached and a note saying the budget was exceeded —
  unless an earlier attempt already reached ``good``, in which case the pair
  would have returned already.

**The manual-alignment exception (documented, deliberate).** The plan's
manual workflow (B7) lets a user click two to four matching points. The
quality gate cannot cross-validate such a fit — its hold-out rule refuses
below ``min_anchors + 2`` (5 by default) reference points, and with two
points there is no anchor count or spread to measure either. A user who
explicitly placed the points is the ground truth, so :func:`apply_manual`
overrides a ``failed`` assessment to ``good`` *only* when every refusal is
one of those by-rule, unmeasurable checks (hold-out on too few anchors,
anchor count, anchor spread), the post-warp ink overlap passes (>= 0.6) and
the transform sanity checks pass. The override is never applied when ink or
sanity fails, and the raw gate verdict stays visible on the result's
``assessment``; the waiver is spelled out in the result note.

Nothing in this module ever raises for a failed alignment: an unreadable
sheet or a crashing method becomes a ``failed`` :class:`AlignmentResult`
with the reason in the note. Loading each side happens once per side and is
shared by every method.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import cv2
import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.align import quality
from engine.align.anchors import extract_text_anchors, find_correspondences, normalise_anchor_text
from engine.align.grid_bubbles import detect_bubbles, match_bubbles
from engine.align.image_align import align_features, align_phase_correlation, align_sheet_border
from engine.align.refine import refine_ecc
from engine.align.transform import fit_transform
from engine.align.types import (
    AlignConfig,
    AlignMethod,
    Correspondence,
    QualityAssessment,
    TransformModel,
    TransformResult,
    Verdict,
)
from engine.align.units import expected_content_scale
from engine.core.jobs import CancelToken
from engine.extract.raster_renderer import RenderOptions, detect_scanned, render_page
from engine.extract.text_extractor import PageText, extract_document_text
from engine.utils.pdf_runtime import open_document

#: Verdict rank for picking the best attempt: excellent > good > poor > failed.
_VERDICT_RANK: dict[Verdict, int] = {
    Verdict.EXCELLENT: 4,
    Verdict.GOOD: 3,
    Verdict.POOR: 2,
    Verdict.FAILED: 1,
}

#: Drawing-scale patterns the title block may spell the scale as.
_SCALE_PATTERN = re.compile(r"(?:^|\D)1\s*[:/]\s*(\d{1,6})(?:\D|$)")

# ── Public records ────────────────────────────────────────────────────────


@dataclass(slots=True)
class SheetRef:
    """One side of a pair to align: a PDF page plus optional metadata."""

    abs_path: str
    page_index: int = 0
    #: The drawing scale as printed in the title block ("1 : 100"), parsed by
    #: :func:`_scale_denominator` for the metadata prior and mm-on-site
    #: conversions of the new sheet.
    scale_text: str | None = None
    revision: str | None = None

    @property
    def name(self) -> str:
        """The file name for notes and progress labels."""
        return os.path.basename(self.abs_path)


@dataclass(slots=True)
class AttemptRecord:
    """One timed attempt: a method, its gate verdict and why it did not win.

    Every attempt of a pair is recorded, successful ones too — the review
    screen shows the whole cascade. ``matrix`` is the accepted transform (or
    None when the attempt produced nothing usable) and ``detail`` reuses the
    quality gate's plain-English explanation so pixels never leak to users.
    """

    method: AlignMethod
    verdict: Verdict
    #: Why this attempt did not win; empty when it passed the gate.
    reason: str
    duration_s: float
    matrix: NDArray[np.float64] | None
    detail: str


@dataclass(slots=True)
class AlignmentResult:
    """The decision for one pair: verdict, winner, and the whole story."""

    verdict: Verdict
    #: The method that produced ``matrix``; None only when nothing ran (an
    #: unreadable pair). On a failed pair this is the best attempt's method.
    method: AlignMethod | None
    matrix: NDArray[np.float64] | None
    #: The quality gate's assessment of the winning (or best) attempt. On a
    #: waived manual fit this still carries the raw gate verdict.
    assessment: QualityAssessment | None
    attempts: list[AttemptRecord] = field(default_factory=list)
    duration_s: float = 0.0
    note: str = ""


# ── Private records ───────────────────────────────────────────────────────


@dataclass(slots=True)
class _Prepared:
    """One side of a pair, loaded once and shared by every cascade method."""

    image: NDArray[np.uint8]  # full-resolution grayscale at config.dpi
    #: The same render at config.coarse_scale, kept for callers that want a
    #: pre-downsampled copy (the current cascade passes full-resolution
    #: images everywhere: thin line art loses its ink below ~0.5 px/line,
    #: which 25% downsamples of real sheets already approach).
    coarse: NDArray[np.uint8]
    #: Extracted text layer of the page; None when extraction failed.
    text: PageText | None
    px_per_mm: float
    mm_width: float
    mm_height: float
    scanned: bool
    dpi: int


@dataclass(slots=True)
class _Assessed:
    """The result of one fit+refine+assess run, before bookkeeping."""

    transform: TransformResult | None
    assessment: QualityAssessment | None
    #: Extra prose for the attempt detail (ECC outcome, escalation, ...).
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Ran:
    """A timed attempt: its record plus the artifacts for the caller."""

    record: AttemptRecord
    transform: TransformResult | None
    assessment: QualityAssessment | None


# ── Scale metadata ────────────────────────────────────────────────────────


def _scale_denominator(scale_text: str | None) -> int | None:
    """Parse the drawing scale denominator out of title-block text.

    Accepts the spellings seen on real sheets — ``1 : 100``, ``1:100``,
    ``1/100``, including surrounding words like ``SCALE 1:50``. Returns None
    when the text is absent or carries no such pattern; the caller falls
    back to the config's known denominators.
    """
    if not scale_text:
        return None
    match = _SCALE_PATTERN.search(scale_text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _scale_denominator_for(ref: SheetRef, config: AlignConfig) -> int | None:
    """The new sheet's drawing scale: parsed text wins over config."""
    parsed = _scale_denominator(ref.scale_text)
    return parsed if parsed is not None else config.new_scale_denominator


def _expected_content_scale(
    old: SheetRef,
    new: SheetRef,
    old_prep: _Prepared,
    new_prep: _Prepared,
    config: AlignConfig,
) -> float | None:
    """Metadata prior (plan B4 method 6) from scales and sheet sizes.

    Diagnostic only — see the module docstring: mismatches are recorded into
    attempt details, never used to refuse a fit.
    """
    old_den = _scale_denominator(old.scale_text)
    if old_den is None:
        old_den = config.old_scale_denominator
    new_den = _scale_denominator(new.scale_text)
    if new_den is None:
        new_den = config.new_scale_denominator
    return expected_content_scale(
        old_den,
        new_den,
        old_prep.mm_width * old_prep.mm_height,
        new_prep.mm_width * new_prep.mm_height,
    )


# ── Loading one side ──────────────────────────────────────────────────────


def _is_scanned(path: str, page_index: int) -> bool:
    """True when the page looks like a photocopy (one big image, no text)."""
    try:
        with open_document(path) as document:
            if page_index < 0 or page_index >= len(document):
                return False
            return bool(detect_scanned(document[page_index]))
    except Exception as exc:  # an unreadable page is not scanned evidence
        logger.debug("scanned check failed for {} page {}: {}", path, page_index, exc)
        return False


def _extract_page_text(path: str, page_index: int) -> PageText | None:
    """The page's text layer, or None when it cannot be read."""
    try:
        pages = extract_document_text(path, pages=[page_index])
    except Exception as exc:  # extract_document_text already swallows pdfium errors
        logger.debug("text extraction failed for {} page {}: {}", path, page_index, exc)
        return None
    return pages.get(page_index)


def _text_from_provider(text_for: object, ref: SheetRef) -> PageText | None:
    """Pull pre-extracted text from the caller's ``text_for`` hook.

    Accepted forms: a callable taking the :class:`SheetRef` and returning a
    :class:`PageText` or None, or a mapping keyed by the absolute path whose
    values are :class:`PageText` objects. Anything else yields None and the
    caller extracts the text layer itself.
    """
    try:
        if callable(text_for):
            supplied = text_for(ref)
        else:
            getter = getattr(text_for, "get", None)
            if callable(getter):
                supplied = getter(ref.abs_path)
            else:
                return None
    except Exception as exc:
        logger.debug("text_for provider failed for {}: {}", ref.abs_path, exc)
        return None
    return supplied if isinstance(supplied, PageText) else None


def _prepare_side(
    ref: SheetRef, config: AlignConfig, *, text_for: object | None = None
) -> _Prepared:
    """Render, extract text from and scan-detect one sheet, once per side.

    The full-resolution grayscale render, its coarse copy, the text layer and
    the scanned flag are all loaded here so the cascade never touches the
    PDF again. Raises when the page cannot be rendered.
    """
    render = render_page(
        ref.abs_path,
        ref.page_index,
        options=RenderOptions(
            dpi=config.dpi,
            colour=False,
            grayscale_for_compare=True,
            memory_budget_mb=config.render_memory_budget_mb,
        ),
    )
    image = render.grayscale
    if image is None:
        raise RuntimeError("the renderer returned no grayscale image")
    height, width = image.shape
    small_w = max(1, round(width * config.coarse_scale))
    small_h = max(1, round(height * config.coarse_scale))
    coarse = cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA)

    page: PageText | None = None
    if text_for is not None:
        page = _text_from_provider(text_for, ref)
    if page is None:
        page = _extract_page_text(ref.abs_path, ref.page_index)

    return _Prepared(
        image=image,
        coarse=coarse,
        text=page,
        px_per_mm=render.px_per_mm,
        mm_width=render.mm_width,
        mm_height=render.mm_height,
        scanned=_is_scanned(ref.abs_path, ref.page_index),
        dpi=config.dpi,
    )


def _bubble_label_for(
    prep: _Prepared, config: AlignConfig
) -> Callable[[tuple[float, float]], str | None] | None:
    """A ``label_for`` callback that reads grid-bubble labels from the text layer.

    ``detect_bubbles`` calls the callback with a bubble centre in render
    pixels; this converts it back to PDF points (the inverse of
    ``anchors.page_points_to_image_px``) and returns the normalised text of
    the nearest extracted item whose centre falls within the configured
    bubble radius. Returns None when the side has no text layer at all, in
    which case bubbles cannot be labelled (there is no OCR provider in this
    project) and will not match — the fixtures' decorative circles are
    label-less exactly this way, so the bubbles step yields nothing and the
    cascade moves on, which is the correct behaviour.
    """
    page = prep.text
    if page is None or not page.items:
        return None
    scale = prep.dpi / 72.0  # render pixels per PDF point
    radius_pt = (config.bubble_min_mm + config.bubble_max_mm) / 2.0 * 72.0 / 25.4
    box = page.box
    items = list(page.items)
    centres = [(item.x + item.width / 2.0, item.y + item.height / 2.0) for item in items]

    def label_for(centre_px: tuple[float, float]) -> str | None:
        px, py = centre_px
        x_pt = box.x0 + px / scale
        y_pt = box.y0 + box.height - py / scale
        best_index = -1
        best_distance = math.inf
        for index, (item_x, item_y) in enumerate(centres):
            distance = math.hypot(x_pt - item_x, y_pt - item_y)
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index < 0 or best_distance > radius_pt:
            return None
        return normalise_anchor_text(items[best_index].text) or None

    return label_for


# ── Attempt machinery ─────────────────────────────────────────────────────


def _record_for(
    method: AlignMethod,
    *,
    verdict: Verdict,
    reason: str,
    duration_s: float,
    matrix: NDArray[np.float64] | None,
    detail: str,
) -> AttemptRecord:
    return AttemptRecord(
        method=method,
        verdict=verdict,
        reason=reason,
        duration_s=duration_s,
        matrix=matrix,
        detail=detail,
    )


def _run_attempt(
    method: AlignMethod,
    runnable: Callable[[], _Assessed],
) -> _Ran:
    """Time one cascade attempt; an attempt never crashes the cascade.

    Exceptions inside the runnable become a failed attempt record with the
    exception text as the reason — never a raise.
    """
    started = time.perf_counter()
    try:
        outcome = runnable()
    except Exception as exc:
        logger.warning("Alignment attempt {} raised: {}", method, exc)
        duration = time.perf_counter() - started
        text = str(exc)
        return _Ran(
            record=_record_for(
                method,
                verdict=Verdict.FAILED,
                reason=f"the {method.label()} step failed internally",
                duration_s=duration,
                matrix=None,
                detail=text,
            ),
            transform=None,
            assessment=None,
        )
    duration = time.perf_counter() - started
    transform = outcome.transform
    assessment = outcome.assessment
    if assessment is not None:
        if assessment.verdict.proceeds_automatically:
            reason = ""
        elif assessment.failures:
            reason = assessment.failures[0]
        else:
            reason = assessment.suggestion or "the fit did not pass the quality gate"
        detail = " ".join(part for part in [*outcome.notes, assessment.explanation] if part)
        matrix = None if transform is None else np.asarray(transform.matrix, dtype=float).copy()
    elif transform is not None and transform.note:
        note = transform.note
        reason = note
        detail = note
        matrix = np.asarray(transform.matrix, dtype=float).copy()
    else:
        reason = "no usable transform was produced"
        detail = reason
        matrix = None
    return _Ran(
        record=_record_for(
            method,
            verdict=assessment.verdict if assessment is not None else Verdict.FAILED,
            reason=reason,
            duration_s=duration,
            matrix=matrix,
            detail=detail,
        ),
        transform=transform,
        assessment=assessment,
    )


def _assess_with_fallback(
    coarse: TransformResult,
    refined: TransformResult,
    correspondences: list[Correspondence],
    old_img: np.ndarray,
    new_img: np.ndarray,
    config: AlignConfig,
    *,
    px_per_mm: float,
    scale_denominator: int | None,
    is_scanned: bool,
    notes: list[str],
) -> tuple[TransformResult, QualityAssessment]:
    """Assess the refined matrix; fall back to the coarse fit when it passes.

    ECC refinement is advisory: its chamfer acceptance rule can be fooled by
    content that partially leaves the canvas (the chamfer then has a flat,
    misleading basin), so a refinement that the quality gate refuses is
    re-checked against the unrefined fit — the gate is the arbiter, and a
    coarse fit the gate certifies is never thrown away for a refinement it
    refuses. Returns (chosen transform, its assessment); when both fail the
    refined one is kept for the record.
    """
    refined_assessment = quality.assess(
        refined,
        correspondences,
        old_img,
        new_img,
        config,
        px_per_mm=px_per_mm,
        scale_denominator=scale_denominator,
        is_scanned=is_scanned,
    )
    if refined_assessment.verdict.proceeds_automatically:
        return refined, refined_assessment
    coarse_assessment = quality.assess(
        coarse,
        correspondences,
        old_img,
        new_img,
        config,
        px_per_mm=px_per_mm,
        scale_denominator=scale_denominator,
        is_scanned=is_scanned,
    )
    if coarse_assessment.verdict.proceeds_automatically:
        notes.append(
            "ECC refinement was applied but the quality gate refused the "
            "refined matrix; the coarse fit passes the gate and is kept instead."
        )
        return coarse, coarse_assessment
    return refined, refined_assessment


def _run_assessed(
    method: AlignMethod,
    old_prep: _Prepared,
    new_prep: _Prepared,
    config: AlignConfig,
    *,
    correspondences: list[Correspondence],
    model: TransformModel,
    fit: Callable[[], TransformResult],
    is_scanned: bool,
    scale_denominator: int | None,
    expected_scale: float | None,
    notes: list[str] | None = None,
) -> _Ran:
    """Fit -> refine -> assess as one attempt, via :func:`_run_attempt`."""

    def runnable() -> _Assessed:
        transform = fit()
        if not math.isfinite(transform.rms_residual_px):
            # The fit refused (too few points, degenerate geometry, size
            # mismatch for the image methods): its note is the reason.
            return _Assessed(transform=transform, assessment=None)
        coarse = replace(transform, matrix=np.asarray(transform.matrix, dtype=float).copy())
        extra = list(notes or [])
        assessment: QualityAssessment | None = None
        if old_prep.image.shape == new_prep.image.shape:
            # ECC needs equal-size template and input; skipped otherwise.
            refined = refine_ecc(
                old_prep.image, new_prep.image, coarse.matrix, model=model, config=config
            )
            if refined.applied:
                extra.append(
                    "ECC refinement applied (ink-distance RMS "
                    f"{refined.rms_before_px:.2f} -> {refined.rms_after_px:.2f} px)."
                )
                working = replace(coarse, matrix=np.asarray(refined.matrix, dtype=float))
                working, assessment = _assess_with_fallback(
                    coarse,
                    working,
                    correspondences,
                    old_prep.image,
                    new_prep.image,
                    config,
                    px_per_mm=old_prep.px_per_mm,
                    scale_denominator=scale_denominator,
                    is_scanned=is_scanned,
                    notes=extra,
                )
            else:
                working = coarse
        else:
            working = coarse
        if assessment is None:
            assessment = quality.assess(
                working,
                correspondences,
                old_prep.image,
                new_prep.image,
                config,
                px_per_mm=old_prep.px_per_mm,
                scale_denominator=scale_denominator,
                is_scanned=is_scanned,
            )
        if correspondences and expected_scale is not None and expected_scale > 0.0:
            mismatch = abs(working.scale - expected_scale) / expected_scale
            if mismatch > config.expected_scale_tolerance:
                extra.append(
                    "Metadata prior, recorded not a refusal: the fitted scale "
                    f"{working.scale:.3f}x differs from the {expected_scale:.3f}x "
                    "the title-block scales and sheet sizes predict."
                )
        return _Assessed(transform=working, assessment=assessment, notes=extra)

    return _run_attempt(method, runnable)


def _skipped_run(method: AlignMethod, reason: str) -> _Ran:
    """An attempt record for a method that had nothing to fit."""
    return _Ran(
        record=_record_for(
            method,
            verdict=Verdict.FAILED,
            reason=reason,
            duration_s=0.0,
            matrix=None,
            detail=reason,
        ),
        transform=None,
        assessment=None,
    )


def _ran_rank(run: _Ran) -> tuple[int, int]:
    """Best-attempt ordering: verdict first, usable matrix second."""
    return (_VERDICT_RANK[run.record.verdict], 1 if run.record.matrix is not None else 0)


def _result_from_winner(
    old: SheetRef,
    new: SheetRef,
    run: _Ran,
    attempts: list[AttemptRecord],
    started: float,
) -> AlignmentResult:
    """Build the successful result; earlier failed attempts get explained."""
    elapsed = time.perf_counter() - started
    explanation = run.assessment.explanation if run.assessment is not None else ""
    earlier = attempts[:-1]
    tried = (
        ""
        if not earlier
        else " Earlier methods that did not pass the gate: "
        + "; ".join(f"{item.method.label()} — {item.reason}" for item in earlier)
        + "."
    )
    assert run.record.method is not None
    note = (
        f"Aligned {old.name} onto {new.name} in {elapsed:.1f} s using "
        f"{run.record.method.label()}. {explanation}{tried}"
    )
    return AlignmentResult(
        verdict=run.record.verdict,
        method=run.record.method,
        matrix=run.record.matrix,
        assessment=run.assessment,
        attempts=attempts,
        duration_s=elapsed,
        note=note,
    )


# ── The pair cascade ──────────────────────────────────────────────────────


def align_pair(
    old: SheetRef,
    new: SheetRef,
    config: AlignConfig | None = None,
    *,
    text_for: object | None = None,
) -> AlignmentResult:
    """Align one pair of sheets through the strategy cascade.

    Loads each side once (render + text layer + scanned flag), computes the
    metadata scale prior, then tries grid bubbles, text anchors, phase
    correlation, SIFT features and finally sheet-border corners, refining
    every fit with ECC and stopping at the first attempt the quality gate
    rates ``good`` or ``excellent``. When nothing passes, or the time budget
    (``config.pair_timeout_s``) runs out, the result verdict is ``failed``
    with the best attempt attached and a note explaining the path — never a
    raise. ``text_for`` optionally supplies pre-extracted text layers (see
    :func:`_text_from_provider`).
    """
    cfg = config if config is not None else AlignConfig()
    started = time.perf_counter()
    deadline = started + max(0.0, float(cfg.pair_timeout_s))

    def remaining() -> float:
        return deadline - time.perf_counter()

    try:
        old_prep = _prepare_side(old, cfg, text_for=text_for)
        new_prep = _prepare_side(new, cfg, text_for=text_for)
    except Exception as exc:
        logger.warning("Could not prepare alignment pair {} vs {}: {}", old.name, new.name, exc)
        return AlignmentResult(
            verdict=Verdict.FAILED,
            method=None,
            matrix=None,
            assessment=None,
            duration_s=time.perf_counter() - started,
            note=f"Could not load {old.name} or {new.name}: {exc}.",
        )

    is_scanned = old_prep.scanned or new_prep.scanned
    scale_denominator = _scale_denominator_for(new, cfg)
    expected_scale = _expected_content_scale(old, new, old_prep, new_prep, cfg)
    if is_scanned:
        logger.debug(
            "pair {} vs {}: scanned sheet detected (old={}, new={})",
            old.name,
            new.name,
            old_prep.scanned,
            new_prep.scanned,
        )

    attempts: list[AttemptRecord] = []
    best: _Ran | None = None

    def file_attempt(run: _Ran) -> None:
        """Record an attempt and remember the best one seen so far."""
        attempts.append(run.record)
        nonlocal best
        if best is None or _ran_rank(run) > _ran_rank(best):
            best = run

    def result_failure(reason: str) -> AlignmentResult:
        """FAILED pair result: budget out, or no method passed the gate."""
        elapsed = time.perf_counter() - started
        fallback = best
        return AlignmentResult(
            verdict=Verdict.FAILED,
            method=fallback.record.method if fallback is not None else None,
            matrix=fallback.record.matrix if fallback is not None else None,
            assessment=fallback.assessment if fallback is not None else None,
            attempts=attempts,
            duration_s=elapsed,
            note=reason,
        )

    # (a) Grid bubbles — the construction-specific anchor of choice.
    if remaining() > 0.0:
        old_bubbles: list[Any] = []
        new_bubbles: list[Any] = []
        try:
            old_bubbles = detect_bubbles(
                old_prep.image,
                old_prep.px_per_mm,
                cfg,
                label_for=_bubble_label_for(old_prep, cfg),
            )
            new_bubbles = detect_bubbles(
                new_prep.image,
                new_prep.px_per_mm,
                cfg,
                label_for=_bubble_label_for(new_prep, cfg),
            )
        except Exception as exc:
            logger.debug("grid bubble detection failed for {} vs {}: {}", old.name, new.name, exc)
        bubble_correspondences = match_bubbles(old_bubbles, new_bubbles)
        if len(bubble_correspondences) >= 6:
            run = _run_assessed(
                AlignMethod.GRID_BUBBLES,
                old_prep,
                new_prep,
                cfg,
                correspondences=bubble_correspondences,
                model=TransformModel.SIMILARITY,
                fit=lambda: fit_transform(
                    bubble_correspondences,
                    TransformModel.SIMILARITY,
                    config=cfg,
                    method_name=AlignMethod.GRID_BUBBLES,
                ),
                is_scanned=is_scanned,
                scale_denominator=scale_denominator,
                expected_scale=expected_scale,
            )
            file_attempt(run)
            if run.record.verdict.proceeds_automatically:
                return _result_from_winner(old, new, run, attempts, started)
            if is_scanned and len(bubble_correspondences) >= 3 and remaining() > 0.0:
                affine_run = _run_assessed(
                    AlignMethod.GRID_BUBBLES,
                    old_prep,
                    new_prep,
                    cfg,
                    correspondences=bubble_correspondences,
                    model=TransformModel.AFFINE,
                    fit=lambda: fit_transform(
                        bubble_correspondences,
                        TransformModel.AFFINE,
                        config=cfg,
                        method_name=AlignMethod.GRID_BUBBLES,
                    ),
                    is_scanned=is_scanned,
                    scale_denominator=scale_denominator,
                    expected_scale=expected_scale,
                    notes=["Affine escalation: a sheet is detected as scanned."],
                )
                file_attempt(affine_run)
                if affine_run.record.verdict.proceeds_automatically:
                    return _result_from_winner(old, new, affine_run, attempts, started)
        else:
            reason = (
                f"Grid bubbles: {len(old_bubbles)} detected on the old sheet and "
                f"{len(new_bubbles)} on the new, but only "
                f"{len(bubble_correspondences)} carried matching labels (need at least 6)."
            )
            file_attempt(_skipped_run(AlignMethod.GRID_BUBBLES, reason))

    # (b) Unique text anchors — best for CAD-exported PDFs.
    if remaining() > 0.0:
        old_page = old_prep.text
        new_page = new_prep.text
        old_anchors = extract_text_anchors(old_page, cfg.dpi) if old_page is not None else []
        new_anchors = extract_text_anchors(new_page, cfg.dpi) if new_page is not None else []
        matched = find_correspondences(old_anchors, new_anchors)
        if len(matched.correspondences) >= 3:
            anchor_correspondences = matched.correspondences
            run = _run_assessed(
                AlignMethod.TEXT_ANCHORS,
                old_prep,
                new_prep,
                cfg,
                correspondences=anchor_correspondences,
                model=TransformModel.SIMILARITY,
                fit=lambda: fit_transform(
                    anchor_correspondences,
                    TransformModel.SIMILARITY,
                    config=cfg,
                    method_name=AlignMethod.TEXT_ANCHORS,
                ),
                is_scanned=is_scanned,
                scale_denominator=scale_denominator,
                expected_scale=expected_scale,
            )
            file_attempt(run)
            if run.record.verdict.proceeds_automatically:
                return _result_from_winner(old, new, run, attempts, started)
            if is_scanned and len(anchor_correspondences) >= 3 and remaining() > 0.0:
                affine_run = _run_assessed(
                    AlignMethod.TEXT_ANCHORS,
                    old_prep,
                    new_prep,
                    cfg,
                    correspondences=anchor_correspondences,
                    model=TransformModel.AFFINE,
                    fit=lambda: fit_transform(
                        anchor_correspondences,
                        TransformModel.AFFINE,
                        config=cfg,
                        method_name=AlignMethod.TEXT_ANCHORS,
                    ),
                    is_scanned=is_scanned,
                    scale_denominator=scale_denominator,
                    expected_scale=expected_scale,
                    notes=["Affine escalation: a sheet is detected as scanned."],
                )
                file_attempt(affine_run)
                if affine_run.record.verdict.proceeds_automatically:
                    return _result_from_winner(old, new, affine_run, attempts, started)
        else:
            reason = (
                "Text anchors: only "
                f"{len(matched.correspondences)} unique strings matched exactly "
                f"once on both sheets ({matched.old_total} found on the old sheet, "
                f"{matched.new_total} on the new; need at least {cfg.min_anchors})."
            )
            file_attempt(_skipped_run(AlignMethod.TEXT_ANCHORS, reason))

    # (c) Phase correlation — global, and the strongest scan fallback.
    if remaining() > 0.0:
        run = _run_assessed(
            AlignMethod.PHASE_CORRELATION,
            old_prep,
            new_prep,
            cfg,
            correspondences=[],
            model=TransformModel.SIMILARITY,
            fit=lambda: align_phase_correlation(
                old_prep.image, new_prep.image, coarse=cfg.coarse_scale, config=cfg
            ),
            is_scanned=is_scanned,
            scale_denominator=scale_denominator,
            expected_scale=None,
        )
        file_attempt(run)
        if run.record.verdict.proceeds_automatically:
            return _result_from_winner(old, new, run, attempts, started)

    # (d) SIFT features — the honest fallback for line art.
    if remaining() > 0.0:
        run = _run_assessed(
            AlignMethod.FEATURES,
            old_prep,
            new_prep,
            cfg,
            correspondences=[],
            model=TransformModel.SIMILARITY,
            fit=lambda: align_features(old_prep.image, new_prep.image, config=cfg),
            is_scanned=is_scanned,
            scale_denominator=scale_denominator,
            expected_scale=None,
        )
        file_attempt(run)
        if run.record.verdict.proceeds_automatically:
            return _result_from_winner(old, new, run, attempts, started)

    # (e) Sheet-border corners — last resort, frame not content, and only a
    #     gate-clearing fit (good/excellent) is ever accepted here.
    if remaining() > 0.0:
        run = _run_assessed(
            AlignMethod.SHEET_BORDER,
            old_prep,
            new_prep,
            cfg,
            correspondences=[],
            model=TransformModel.SIMILARITY,
            fit=lambda: align_sheet_border(old_prep.image, new_prep.image),
            is_scanned=is_scanned,
            scale_denominator=scale_denominator,
            expected_scale=None,
        )
        file_attempt(run)
        if run.record.verdict.proceeds_automatically:
            return _result_from_winner(old, new, run, attempts, started)

    # (f) Nothing passed. Refuse honestly, with the best attempt attached.
    if remaining() <= 0.0:
        reason = (
            f"Time budget exceeded: the {cfg.pair_timeout_s:g} s per-pair budget ran out "
            f"after {len(attempts)} of the automatic methods. "
        )
        if best is not None and best.assessment is not None:
            reason += (
                f"The best attempt ({best.record.method.label()}) scored "
                f"{best.assessment.verdict.value} — {best.assessment.explanation} "
            )
        reason += "Align this pair manually or retry with a longer budget."
        return result_failure(reason)

    tried = "; ".join(f"{item.method.label()} — {item.reason or 'failed'}" for item in attempts)
    note = (
        "None of the automatic methods passed the quality gate. Align this pair "
        f"manually. Tried: {tried}."
    )
    return result_failure(note)


# ── Batch ─────────────────────────────────────────────────────────────────


def align_batch(
    pairs: Sequence[tuple[SheetRef, SheetRef]],
    config: AlignConfig | None = None,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: object | None = None,
) -> list[AlignmentResult]:
    """Align a list of pairs sequentially, reporting progress and honouring cancel.

    Sequential by design: pdfium is not thread-safe within one process, so
    pool parallelism belongs to the queue/batch layer of a later task, where
    every worker process owns its own pdfium. ``progress(completed, total,
    label)`` is called before and after every pair; ``cancel`` is anything
    exposing a ``cancelled`` property (e.g. :class:`engine.core.jobs.CancelToken`)
    and is checked between pairs, so a cancelled run keeps its completed
    results and simply stops.
    """
    token = cancel if cancel is not None else CancelToken()
    results: list[AlignmentResult] = []
    total = len(pairs)
    for completed, (old_ref, new_ref) in enumerate(pairs):
        if getattr(token, "cancelled", False):
            break
        label = f"{old_ref.name} -> {new_ref.name}"
        if progress is not None:
            try:
                progress(completed, total, label)
            except Exception as exc:
                logger.warning("progress callback raised: {}", exc)
        results.append(align_pair(old_ref, new_ref, config))
        if progress is not None:
            try:
                progress(completed + 1, total, label)
            except Exception as exc:
                logger.warning("progress callback raised: {}", exc)
    return results


# ── Manual alignment ──────────────────────────────────────────────────────


def _manual_waiver_applies(assessment: QualityAssessment, count: int, config: AlignConfig) -> bool:
    """True when a manual fit may be accepted despite a failed gate.

    The waiver is deliberately narrow: the gate verdict must be ``failed``,
    the set must be too small for cross-validation to run at all (below
    ``min_anchors + 2``, so any hold-out refusal is the by-rule one), every
    failing metric must be one the set size makes unmeasurable (hold-out on
    too few anchors, anchor count, anchor spread), and the two checks that
    carry real evidence — post-warp ink overlap and transform sanity — must
    both pass. Never waived on ink or sanity failures, whatever the user
    clicked.
    """
    if assessment.verdict is not Verdict.FAILED:
        return False
    if count >= config.min_anchors + 2:
        return False  # cross-validation ran and really failed: no waiver
    metrics = assessment.metrics
    if not metrics[quality.INK_OVERLAP][2]:
        return False
    if not metrics[quality.TRANSFORM_SANITY][2]:
        return False
    failed = {name for name, outcome in metrics.items() if not outcome[2]}
    allowed = {quality.ANCHOR_COUNT, quality.ANCHOR_SPREAD, quality.HOLDOUT_RMS}
    return failed <= allowed


def _manual_waiver_note(assessment: QualityAssessment, count: int) -> str:
    """The plain-English record of a manual waiver, for the result note."""
    overlap = assessment.metrics[quality.INK_OVERLAP][0]
    return (
        f"The quality gate alone would refuse this fit — with only {count} clicked "
        "pairs it cannot hold reference points out for cross-validation (at least "
        "5 are needed), so its refusal here is waived: you placed the points by "
        "hand, the transform passes the sanity checks, and the post-warp ink "
        f"overlap of {overlap:.0%} confirms the sheets line up. The waiver is "
        "never applied when the ink or sanity checks fail."
    )


def apply_manual(
    old_points: Sequence[tuple[float, float]],
    new_points: Sequence[tuple[float, float]],
    config: AlignConfig | None = None,
    *,
    old_img: np.ndarray,
    new_img: np.ndarray,
    scale_denominator: int | None = None,
    px_per_mm: float = 200 / 25.4,
) -> AlignmentResult:
    """Fit a manual alignment from user-clicked point pairs.

    Two pairs determine a similarity exactly; three or more are fitted with a
    weighted least-squares similarity whose residual can be reported. The fit
    is ECC-refined (when the images share dimensions) and measured by the
    same quality gate as every automatic method, with ``method`` set to
    :attr:`AlignMethod.MANUAL`.

    The manual exception documented in the module docstring applies: a fit
    the gate refuses *only* because fewer than ``min_anchors + 2`` points
    leave nothing to cross-validate (plus the anchor-count/spread checks the
    same small set cannot satisfy) is accepted with a ``good`` verdict when
    the ink-overlap and sanity checks pass, and the waiver is spelled out in
    the note. The raw gate verdict stays on ``result.assessment``. Raises
    :class:`ValueError` for fewer than two pairs or mismatched lists; a fit
    that cannot be determined, or one the gate refuses on real evidence, is
    returned as a ``failed`` result, never raised.
    """
    config = config if config is not None else AlignConfig()
    old_list = list(old_points)
    new_list = list(new_points)
    if len(old_list) != len(new_list):
        raise ValueError(
            f"manual alignment needs matching point lists, got {len(old_list)} old "
            f"and {len(new_list)} new points"
        )
    if len(old_list) < 2:
        raise ValueError("manual alignment needs at least 2 matching point pairs")
    started = time.perf_counter()
    correspondences = [
        Correspondence(
            old_x=old_x,
            old_y=old_y,
            new_x=new_x,
            new_y=new_y,
            weight=1.0,
            label=f"manual point {index}",
        )
        for index, ((old_x, old_y), (new_x, new_y)) in enumerate(
            zip(old_list, new_list, strict=True)
        )
    ]

    fit = fit_transform(
        correspondences,
        TransformModel.SIMILARITY,
        method="ls",
        config=config,
        method_name=AlignMethod.MANUAL,
    )
    if not math.isfinite(fit.rms_residual_px):
        note = fit.note or "the clicked points could not determine a similarity"
        elapsed = time.perf_counter() - started
        record = _record_for(
            AlignMethod.MANUAL,
            verdict=Verdict.FAILED,
            reason=note,
            duration_s=elapsed,
            matrix=None,
            detail=note,
        )
        return AlignmentResult(
            verdict=Verdict.FAILED,
            method=AlignMethod.MANUAL,
            matrix=None,
            assessment=None,
            attempts=[record],
            duration_s=elapsed,
            note=f"Could not align from the clicked points: {note}",
        )

    detail_notes: list[str] = []
    working = replace(fit, matrix=np.asarray(fit.matrix, dtype=float).copy())
    assessment: QualityAssessment | None = None
    if np.asarray(old_img).shape == np.asarray(new_img).shape:
        refined = refine_ecc(
            old_img, new_img, working.matrix, model=TransformModel.SIMILARITY, config=config
        )
        if refined.applied:
            detail_notes.append(
                "ECC refinement applied (ink-distance RMS "
                f"{refined.rms_before_px:.2f} -> {refined.rms_after_px:.2f} px)."
            )
            refined_working = replace(fit, matrix=np.asarray(refined.matrix, dtype=float))
            working, assessment = _assess_with_fallback(
                working,
                refined_working,
                correspondences,
                old_img,
                new_img,
                config,
                px_per_mm=px_per_mm,
                scale_denominator=scale_denominator,
                is_scanned=False,
                notes=detail_notes,
            )
    if assessment is None:
        assessment = quality.assess(
            working,
            correspondences,
            old_img,
            new_img,
            config,
            px_per_mm=px_per_mm,
            scale_denominator=scale_denominator,
            is_scanned=False,
        )
    verdict = assessment.verdict
    waiver = ""
    if _manual_waiver_applies(assessment, len(correspondences), config):
        verdict = Verdict.GOOD
        waiver = _manual_waiver_note(assessment, len(correspondences))

    if verdict.proceeds_automatically:
        reason = "accepted as good" if waiver else "passed the quality gate"
    elif verdict is Verdict.POOR:
        reason = (
            "the borderline checks pass within 10% of their limits — "
            "confirm the alignment before relying on it"
        )
    elif assessment.failures:
        reason = assessment.failures[0]
    else:
        reason = assessment.suggestion or "the fit did not pass the quality gate"

    elapsed = time.perf_counter() - started
    record = _record_for(
        AlignMethod.MANUAL,
        verdict=verdict,
        reason=reason,
        duration_s=elapsed,
        matrix=working.matrix.copy(),
        detail=" ".join(part for part in (*detail_notes, assessment.explanation, waiver) if part),
    )
    note = (
        f"Manual alignment from {len(correspondences)} clicked "
        f"{'pair' if len(correspondences) == 1 else 'pairs'} in {elapsed:.1f} s. "
        f"{assessment.explanation} {waiver}".strip()
    )
    return AlignmentResult(
        verdict=verdict,
        method=AlignMethod.MANUAL,
        matrix=working.matrix.copy(),
        assessment=assessment,
        attempts=[record],
        duration_s=elapsed,
        note=note,
    )
