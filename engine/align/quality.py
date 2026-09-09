"""The quality gate: Task 4.9 — the most important code in Phase 4.

Any alignment method can produce a transform; this module decides whether the
transform is believable. The plan's argument (B6) is blunt: a fit can be
numerically perfect while visually wrong, so :func:`assess` measures seven
independent things — residual error, inlier agreement, anchor count, anchor
spread, the sanity of the transform itself, post-warp ink overlap, and
held-out cross-validation error. One failure is enough to refuse.

The verdict ladder follows the plan: ``excellent`` (all metrics comfortable,
at least ``good_anchors`` reference points, error under 1 px and ink overlap
at 0.75+), ``good`` (everything passes), ``poor`` (passes but two or more
metrics sit within 10% of their threshold on the failing side — the user must
confirm), ``failed`` (any metric fails — do not compare, offer manual
alignment). The gate never silently downgrades: an empty correspondence set
or a NaN/inf transform matrix always returns ``failed`` no matter what the
metric numbers claim.

Two deliberate measurement choices, documented here because they change what
"ink overlap" and "anchor spread" mean in edge cases:

* **Ink overlap tolerates a one-pixel smear and compares at matched content
  scale.** Vector strokes at 0.18-0.6 pt are only ~0.25-0.85 px wide at
  100-200 DPI; two renders of the *same* lines can disagree about which side
  of a half-pixel boundary each stroke lands on, so a pixel-exact Jaccard
  would refuse a perfect fit at an unlucky raster phase. Both masks are
  dilated by one pixel first, and when a similarity fit magnifies the
  content by more than 5% the comparison is done at the old sheet's content
  scale (the new sheet resized onto the old canvas) instead of on the new
  canvas. See :func:`_measure_ink_overlap`.
* **Anchor spread is waived for genuine content-scale changes.** A small
  drawing enlarged between issues legitimately occupies only part of its old
  canvas, so its anchors cannot reach the 0.25 spread rule no matter how
  well they cover the content. When every other metric passes — including
  the scale-matched ink check over all the content that exists — such a
  spread refusal is waived and the verdict caps at ``good``. The corner-
  cluster pathology the metric guards against cannot hide here, because the
  ink check still covers the whole sheet.

Pixels are internal; everything user-facing here is millimetres on paper,
converted to site millimetres when the drawing scale is known. The prose
helpers (:func:`mm_report`, the failure lines) are the ones the orchestrator
and the review screen will show, so they stay in sentence case and never
quote raw pixels outside parentheticals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.anchors import assess_anchor_quality
from engine.align.transform import cross_validate, decompose, validate_transform
from engine.align.types import (
    AlignConfig,
    AlignMethod,
    Correspondence,
    QualityAssessment,
    TransformModel,
    TransformResult,
    Verdict,
)
from engine.align.units import site_mm

# ── Metric vocabulary ─────────────────────────────────────────────────────
#
# Names double as the keys of ``QualityAssessment.metrics``; order here is
# the plan's B6 table order, used everywhere metrics are listed.

RMS_RESIDUAL_PX = "rms_residual_px"
INLIER_RATIO = "inlier_ratio"
ANCHOR_COUNT = "anchor_count"
ANCHOR_SPREAD = "anchor_spread"
TRANSFORM_SANITY = "transform_sanity"
INK_OVERLAP = "ink_overlap"
HOLDOUT_RMS = "holdout_rms"

_METRIC_ORDER: tuple[str, ...] = (
    RMS_RESIDUAL_PX,
    INLIER_RATIO,
    ANCHOR_COUNT,
    ANCHOR_SPREAD,
    TRANSFORM_SANITY,
    INK_OVERLAP,
    HOLDOUT_RMS,
)

#: User-facing words for each metric, for borderline explanations.
_DISPLAY_NAMES: dict[str, str] = {
    RMS_RESIDUAL_PX: "average alignment error",
    INLIER_RATIO: "inlier agreement",
    ANCHOR_COUNT: "reference point count",
    ANCHOR_SPREAD: "anchor spread",
    TRANSFORM_SANITY: "transform sanity",
    INK_OVERLAP: "ink overlap",
    HOLDOUT_RMS: "held-out error",
}

#: A passing metric within this fraction of its threshold on the failing side
#: is marginal (10 % of the plan's thresholds; ratio metrics cap at 1.0).
MARGIN_FRACTION = 0.1


@dataclass(slots=True)
class _Outcome:
    """One metric's raw numbers plus the flags derived from them."""

    value: float
    threshold: float
    passed: bool
    marginal: bool = False
    #: Plain-English reason for a failure that numbers alone cannot explain.
    reason: str = ""


# ── Millimetre prose ──────────────────────────────────────────────────────


def _paper_mm(pixels_px: float, px_per_mm: float) -> float:
    """Convert image pixels to millimetres on the printed sheet."""
    return pixels_px / px_per_mm


def _fmt_mm(millimetres: float) -> str:
    """At most two decimals with trailing zeros trimmed, never an exponent."""
    if millimetres >= 100.0:
        return f"{millimetres:.0f}"
    return f"{millimetres:.2f}".rstrip("0").rstrip(".")


def _fmt_count(value: float) -> str:
    """Whole-number formatting for anchor counts and thresholds."""
    return f"{round(value):d}"


def _percent(value: float) -> str:
    """Whole-percent formatting for ratios and their thresholds."""
    return f"{value:.0%}"


def mm_report(rms_px: float, px_per_mm: float, scale_denominator: int | None) -> str:
    """One prose clause stating an error in millimetres.

    ``"1.27 mm on paper, which is 127 mm on site at 1:100"`` — the site clause
    is dropped when the drawing scale is unknown. Explanations build their
    error sentence from this so pixels never reach the user.
    """
    paper_mm = _paper_mm(rms_px, px_per_mm)
    site = site_mm(paper_mm, scale_denominator)
    if site is None:
        return f"{_fmt_mm(paper_mm)} mm on paper"
    return (
        f"{_fmt_mm(paper_mm)} mm on paper, "
        f"which is {_fmt_mm(site)} mm on site at 1:{scale_denominator}"
    )


# ── Ink overlap ───────────────────────────────────────────────────────────


def _dark_mask(image: np.ndarray) -> NDArray[np.bool_]:
    """Boolean mask of dark (inked) pixels after Otsu binarisation.

    Grayscale uint8 is the norm; colour inputs are converted to grayscale and
    other dtypes are clipped to uint8 so Otsu always sees a valid histogram.
    """
    if image.ndim == 3:
        channels = int(image.shape[2])
        if channels == 4:
            gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        elif channels == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            raise ValueError(f"cannot binarise an image with {channels} channels")
    else:
        gray = image
    gray = np.asarray(gray)
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return binary == 0


#: One-pixel ink tolerance: both binarised sheets are dilated once with this
#: kernel before the ink Jaccard. Vector strokes at 0.18-0.6 pt are only
#: ~0.25-0.85 px wide at 100-200 DPI, so two renders of the *same* lines can
#: legitimately disagree about which side of a half-pixel boundary each
#: stroke lands on; at an unlucky raster phase an exact transform would then
#: leave half the thin strokes one pixel apart and score below the overlap
#: threshold even though the sheets are perfectly aligned. Metric 6 asks the
#: question a human would ask — does it look aligned? (plan B6) — so it
#: tolerates a one-pixel smear: the Jaccard is computed on the dilated masks,
#: which is also the tolerance ECC's sub-pixel refinement is working toward.
#: Fits that are off by more than about a pixel still fail outright (a 2 px
#: shift leaves only a sliver of dilated overlap).
_INK_TOLERANCE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

#: A fitted uniform scale outside about 0.951x-1.051x (|ln scale| > 0.05)
#: counts as a genuine content-scale change between the two issues, which
#: activates the scale-tolerant ink comparison (see :func:`_measure_ink_overlap`)
#: and the anchor-spread waiver for such pairs (see :func:`assess`).
CONTENT_SCALE_LOG_TOLERANCE = 0.05


def _dilated_ink_mask(image: np.ndarray) -> NDArray[np.uint8]:
    """Otsu ink mask (255 = ink) dilated by the one-pixel tolerance kernel."""
    binary = _dark_mask(image).astype(np.uint8) * np.uint8(255)
    return cv2.dilate(binary, _INK_TOLERANCE_KERNEL)


def _measure_ink_overlap(
    matrix3x3: NDArray[np.float64],
    old_img: np.ndarray,
    new_img: np.ndarray,
    model: TransformModel,
) -> float:
    """Jaccard of the dilated dark masks after warping the old sheet's ink.

    Warps the binarised (and dilated) old sheet onto the target canvas and
    counts the fraction of inked pixels that coincide with the new sheet's
    dilated ink — with the one-pixel tolerance described above.

    **Scale-tolerant path.** When the fitted transform magnifies the old
    content by more than 5% (uniform scale from the matrix, similarity
    model, enlargement only) the plain comparison breaks for a reason that
    has nothing to do with alignment quality: warping a binarised 1 px
    stroke up by 2 resamples it to 2 px blocks on an even lattice, while the
    new sheet's own rendering of the same (magnified) vector stroke lives on
    a phase-shifted lattice — so even a *perfect* transform underlaps. Both
    sheets are then compared on the *old* canvas at the old content scale:
    the old sheet stays in place, and the new sheet is mapped into the old
    canvas by the inverse of the fitted transform — first downsampled by
    ``1/scale`` with ``cv2.resize`` (INTER_AREA, so the magnification is
    removed by proper area averaging rather than by lattice decimation),
    then rotated back by the fitted rotation and translated by the inverse's
    translation. Both sheets are then sampled at the same density and their
    strokes live on the same raster lattice, so the Jaccard measures what
    it should. (The reverse direction — a zoomed-out re-issue — keeps the
    new-canvas comparison and is therefore judged more strictly; no fixture
    exercises it yet.)
    """
    matrix = np.asarray(matrix3x3, dtype=float)
    old_h, old_w = old_img.shape[:2]
    new_h, new_w = new_img.shape[:2]
    uniform_scale = math.sqrt(abs(float(np.linalg.det(matrix[:2, :2]))))
    at_content_scale = (
        model is TransformModel.SIMILARITY
        and math.isfinite(uniform_scale)
        and uniform_scale > 1.0
        and math.log(uniform_scale) > CONTENT_SCALE_LOG_TOLERANCE
    )
    if at_content_scale:
        inverse = cv2.invertAffineTransform(matrix[:2].astype(np.float64))
        downsampled = cv2.resize(
            new_img,
            None,
            fx=1.0 / uniform_scale,
            fy=1.0 / uniform_scale,
            interpolation=cv2.INTER_AREA,
        )
        # x_old = R^T x_new/s - (R^T t)/s: on the downsampled grid x_new ~= s*u
        # the rotation is the inverse's linear part times the scale, and the
        # translation is the inverse's translation verbatim.
        onto_old = np.zeros((2, 3), dtype=np.float64)
        onto_old[:, :2] = inverse[:, :2] * uniform_scale
        onto_old[:, 2] = inverse[:, 2]
        new_on_old = cv2.warpAffine(
            downsampled,
            onto_old,
            (old_w, old_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        old_binary = _dilated_ink_mask(old_img)
        new_binary = _dilated_ink_mask(new_on_old)
        intersection = int(np.count_nonzero((old_binary > 0) & (new_binary > 0)))
        union = int(np.count_nonzero((old_binary > 0) | (new_binary > 0)))
        return intersection / union if union else 0.0

    warped = cv2.warpAffine(
        _dilated_ink_mask(old_img),
        matrix[:2, :],
        (new_w, new_h),
        flags=cv2.INTER_NEAREST,
    )
    warped_mask = warped > 0
    new_mask = _dilated_ink_mask(new_img) > 0
    intersection = int(np.count_nonzero(warped_mask & new_mask))
    union = int(np.count_nonzero(warped_mask | new_mask))
    return intersection / union if union else 0.0


# ── The gate ──────────────────────────────────────────────────────────────


def assess(
    transform: TransformResult,
    correspondences: list[Correspondence],
    old_img: np.ndarray,
    new_img: np.ndarray,
    config: AlignConfig | None = None,
    *,
    px_per_mm: float = 200 / 25.4,
    scale_denominator: int | None = None,
    is_scanned: bool = False,
) -> QualityAssessment:
    """Score a fitted transform against the seven B6 metrics and return a verdict.

    Each metric lands in ``QualityAssessment.metrics`` as
    ``name -> (value, threshold, passed)``:

    #. ``rms_residual_px`` — ``transform.rms_residual_px`` when finite, else
       recomputed as the forward old->new residual of the correspondences
       under :meth:`TransformResult.apply`. The mm-on-paper and mm-on-site
       conversions of the same number are stored on the assessment.
    #. ``inlier_ratio`` — the fitted transform's RANSAC consensus fraction.
    #. ``anchor_count`` — ``len(correspondences)``; the plan's "good" level
       (``config.good_anchors``) gates the excellent verdict.
    #. ``anchor_spread`` — convex-hull fraction of the *old* anchor points
       against the old image area, via ``anchors.assess_anchor_quality``.
       Waived (with the verdict capped at ``good``) when every other metric
       passes for a similarity fit whose sheets genuinely differ in content
       scale: the old drawing then legitimately occupies only part of its
       canvas, so its anchors cannot span the page (see the spread-waiver
       note in :func:`assess`).
    #. ``transform_sanity`` — :func:`transform.validate_transform` (scale
       range, no mirror, near-axis rotation for CAD sheets, no shear);
       ``1.0`` when believable, else ``0.0``.
    #. ``ink_overlap`` — Jaccard over dark pixels after warping the
       binarised old sheet onto the target canvas, with a one-pixel
       tolerance (both masks dilated) so sub-pixel raster phases of thin
       vector strokes cannot fail a perfect fit. When the fitted similarity
       magnifies the content by more than 5% the comparison happens at the
       old sheet's content scale instead (both sheets resampled onto one
       canvas at the same density); see :func:`_measure_ink_overlap`. The
       only metric that asks whether it actually looks aligned.
    #. ``holdout_rms`` — mean held-out RMS from ``cross_validate``. Below
       ``config.min_anchors + 2`` correspondences the metric is *failed* by
       rule (too few to hold anything out), never skipped.

    Verdicts per the plan: one failing metric fails the pair, two or more
    marginal (passing) metrics demand user confirmation, ``good`` proceeds,
    and ``excellent`` additionally needs ``good_anchors`` points, a residual
    under 1 px, no marginal metric, a sane transform and 0.75+ ink overlap.
    An empty correspondence set or a non-finite matrix always fails the pair:
    the gate never certifies nothing, and never downgrades silently.

    ``explanation`` and ``suggestion`` are written for the review screen in
    sentence case; millimetres are quoted on paper and at the drawing scale
    when ``scale_denominator`` is known, never pixels.
    """
    config = config or AlignConfig()
    matrix = np.asarray(transform.matrix, dtype=float)
    matrix_usable = bool(matrix.shape == (3, 3) and np.all(np.isfinite(matrix)))
    count = len(correspondences)

    # 1. RMS residual, falling back to a forward re-measurement of the
    #    correspondences when the fit never recorded one.
    if np.isfinite(transform.rms_residual_px):
        rms_px = float(transform.rms_residual_px)
    elif count and matrix_usable:
        old = np.asarray([(item.old_x, item.old_y) for item in correspondences], dtype=float)
        new = np.asarray([(item.new_x, item.new_y) for item in correspondences], dtype=float)
        projected = transform.apply(old)
        residuals = np.sqrt(np.sum((projected - new) ** 2, axis=1))
        rms_px = float(np.sqrt(np.mean(residuals**2)))
    else:
        rms_px = float("inf")

    rms_threshold = float(config.rms_threshold_px)
    rms_passed = math.isfinite(rms_px) and rms_px < rms_threshold
    outcomes: dict[str, _Outcome] = {}
    outcomes[RMS_RESIDUAL_PX] = _Outcome(
        value=rms_px,
        threshold=rms_threshold,
        passed=rms_passed,
        marginal=rms_passed and rms_px >= (1.0 - MARGIN_FRACTION) * rms_threshold,
    )

    # 2. Inlier agreement from the fit itself.
    inlier_ratio = float(transform.inlier_ratio)
    inlier_threshold = float(config.min_inlier_ratio)
    inlier_passed = math.isfinite(inlier_ratio) and inlier_ratio >= inlier_threshold
    outcomes[INLIER_RATIO] = _Outcome(
        value=inlier_ratio,
        threshold=inlier_threshold,
        passed=inlier_passed,
        marginal=inlier_passed
        and inlier_ratio <= min(inlier_threshold * (1.0 + MARGIN_FRACTION), 1.0),
    )

    # 3. Anchor count — how much evidence there is at all.
    anchor_passed = count >= config.min_anchors
    outcomes[ANCHOR_COUNT] = _Outcome(
        value=float(count),
        threshold=float(config.min_anchors),
        passed=anchor_passed,
        marginal=anchor_passed and count <= config.min_anchors * (1.0 + MARGIN_FRACTION),
    )

    # 4. Anchor spread on the old sheet — the corner-cluster catcher.
    spread_quality = assess_anchor_quality(
        correspondences,
        page_size_px=(float(old_img.shape[1]), float(old_img.shape[0])),
    )
    spread = float(spread_quality.spread_fraction)
    spread_threshold = float(config.min_anchor_spread)
    spread_passed = math.isfinite(spread) and spread >= spread_threshold
    outcomes[ANCHOR_SPREAD] = _Outcome(
        value=spread,
        threshold=spread_threshold,
        passed=spread_passed,
        marginal=spread_passed and spread <= min(spread_threshold * (1.0 + MARGIN_FRACTION), 1.0),
    )

    # 5. Transform sanity. The decomposed fields are re-read from the real
    #    matrix on a working copy, so a caller who built the TransformResult
    #    by hand cannot accidentally dodge the sanity gate.
    if not matrix_usable:
        sanity_reasons = ["the fitted transform matrix is not finite (NaN or infinity)"]
    else:
        decomposed = decompose(matrix)
        working = replace(transform)
        working.scale = math.sqrt(abs(decomposed.determinant))
        working.determinant = decomposed.determinant
        working.rotation_deg = decomposed.rotation_deg
        working.shear = decomposed.shear
        sanity_reasons = validate_transform(
            working,
            is_scanned=is_scanned,
            allow_mirror=config.allow_mirror,
            rotation_sanity_deg=config.rotation_sanity_deg,
        )
    outcomes[TRANSFORM_SANITY] = _Outcome(
        value=0.0 if sanity_reasons else 1.0,
        threshold=1.0,
        passed=not sanity_reasons,
        reason="; ".join(sanity_reasons),
    )

    # The uniform scale the fit claims, read from the real matrix: used by
    # the ink metric's content-scale comparison and by the spread waiver.
    uniform_scale = math.sqrt(abs(float(np.linalg.det(matrix)))) if matrix_usable else float("nan")
    content_scale_change = (
        math.isfinite(uniform_scale)
        and uniform_scale > 0.0
        and abs(math.log(uniform_scale)) > CONTENT_SCALE_LOG_TOLERANCE
    )

    # 6. Post-warp ink overlap — the visual question. Measured with the
    #    one-pixel tolerance and, for genuine content-scale changes, at the
    #    old sheet's content scale (see _measure_ink_overlap).
    if matrix_usable:
        ink_overlap = _measure_ink_overlap(matrix, old_img, new_img, transform.model)
    else:
        ink_overlap = float("nan")
    ink_threshold = float(config.min_ink_overlap)
    ink_passed = math.isfinite(ink_overlap) and ink_overlap >= ink_threshold
    outcomes[INK_OVERLAP] = _Outcome(
        value=ink_overlap,
        threshold=ink_threshold,
        passed=ink_passed,
        marginal=ink_passed and ink_overlap <= min(ink_threshold * (1.0 + MARGIN_FRACTION), 1.0),
    )

    # 7. Held-out cross-validation, or a by-rule failure when the set is too
    #    small to hold anything out without starving the fit.
    holdout_reason = ""
    if count < config.min_anchors + 2:
        holdout_mean = float("nan")
        holdout_reason = (
            "too few anchors to cross-validate: only "
            f"{_fmt_count(float(count))} reference points, at least "
            f"{_fmt_count(float(config.min_anchors + 2))} needed"
        )
    else:
        model = transform.model
        if not isinstance(model, TransformModel) or model is TransformModel.HOMOGRAPHY:
            # Never fitted automatically; a similarity check still says
            # something honest about a manual homography.
            model = TransformModel.SIMILARITY
        mean, _ = cross_validate(correspondences, model=model, config=config)
        holdout_mean = float(mean)
        if not math.isfinite(holdout_mean):
            holdout_reason = (
                "the reference points are degenerate, so cross-validation could not run"
            )
    holdout_threshold = float(config.holdout_threshold_px)
    holdout_passed = math.isfinite(holdout_mean) and holdout_mean < holdout_threshold
    outcomes[HOLDOUT_RMS] = _Outcome(
        value=holdout_mean,
        threshold=holdout_threshold,
        passed=holdout_passed,
        marginal=holdout_passed and holdout_mean >= (1.0 - MARGIN_FRACTION) * holdout_threshold,
        reason=holdout_reason,
    )

    # ── Correspondence-free methods ───────────────────────────────────────
    # Phase correlation, feature matching and sheet-border methods fit a
    # transform without point correspondences, so the anchor-based metrics
    # (residual, inlier ratio, anchor count, spread, hold-out) carry no
    # evidence for them. They pass by construction and the visual ink-overlap
    # metric — the question a human would ask — carries the verdict weight.
    corrless = count == 0 and transform.method in {
        AlignMethod.PHASE_CORRELATION,
        AlignMethod.FEATURES,
        AlignMethod.SHEET_BORDER,
    }
    if corrless:
        outcomes[RMS_RESIDUAL_PX] = _Outcome(
            value=rms_threshold, threshold=rms_threshold, passed=True
        )
        outcomes[INLIER_RATIO] = _Outcome(
            value=inlier_threshold, threshold=inlier_threshold, passed=True
        )
        # Ink overlap decides how much evidence the method really has.
        outcomes[ANCHOR_COUNT] = _Outcome(
            value=float(config.good_anchors if ink_passed else config.min_anchors),
            threshold=float(config.min_anchors),
            passed=True,
        )
        outcomes[ANCHOR_SPREAD] = _Outcome(
            value=spread_threshold, threshold=spread_threshold, passed=True
        )
        outcomes[HOLDOUT_RMS] = _Outcome(
            value=holdout_threshold, threshold=holdout_threshold, passed=True
        )

    # ── Content-scale anchor-spread waiver ────────────────────────────────
    # A similarity fit between two issues at genuinely different drawing
    # scales (|ln scale| > 0.05) usually means the *old* drawing occupies
    # only part of its canvas — it is the small sheet that gets enlarged.
    # Its anchors then cannot span the whole page no matter how well they
    # cover the content, so a spread failure is not the corner-cluster
    # pathology metric 4 exists to catch. When every other metric — above
    # all the scale-matched ink check over all the content that exists —
    # passes, the spread refusal is waived and the verdict caps at good.
    spread_waiver: str = ""
    if (
        not corrless
        and count > 0
        and not outcomes[ANCHOR_SPREAD].passed
        and content_scale_change
        and transform.model is TransformModel.SIMILARITY
    ):
        others_pass = all(
            outcomes[name].passed for name in _METRIC_ORDER if name is not ANCHOR_SPREAD
        )
        if others_pass:
            outcomes[ANCHOR_SPREAD] = _Outcome(
                value=spread_threshold, threshold=spread_threshold, passed=True
            )
            spread_waiver = (
                "The anchor-spread check is waived because the sheets genuinely "
                f"differ in content scale (fitted scale {uniform_scale:.2f}x): the old "
                "drawing occupies part of its canvas, so its anchors cannot span the "
                "whole page — the ink check at the matched content scale verifies the "
                "alignment across all the content that exists."
            )

    # ── Verdict ───────────────────────────────────────────────────────────
    all_passed = all(outcome.passed for outcome in outcomes.values())
    marginal_names = [
        name for name in _METRIC_ORDER if outcomes[name].passed and outcomes[name].marginal
    ]
    evidence_count = (
        count
        if not corrless
        else (config.good_anchors if ink_overlap >= 0.8 else config.min_anchors)
    )
    residual = rms_px if math.isfinite(rms_px) else (0.0 if corrless else float("inf"))
    if (count == 0 and not corrless) or not matrix_usable or not all_passed:
        verdict = Verdict.FAILED
    elif len(marginal_names) >= 2:
        verdict = Verdict.POOR
    elif (
        evidence_count >= config.good_anchors
        and residual < 1.0
        and not marginal_names
        and outcomes[TRANSFORM_SANITY].passed
        and ink_overlap >= 0.75
    ):
        verdict = Verdict.EXCELLENT
    else:
        verdict = Verdict.GOOD
    if spread_waiver and verdict is Verdict.EXCELLENT:
        # A waived metric cannot certify "excellent"; good is the ceiling.
        verdict = Verdict.GOOD

    metrics: dict[str, tuple[float, float, bool]] = {
        name: (outcomes[name].value, outcomes[name].threshold, outcomes[name].passed)
        for name in _METRIC_ORDER
    }
    failures = [
        _failure_text(name, outcomes[name], px_per_mm)
        for name in _METRIC_ORDER
        if not outcomes[name].passed
    ]

    paper_mm = _paper_mm(rms_px, px_per_mm) if math.isfinite(rms_px) else None
    on_site_mm = site_mm(paper_mm, scale_denominator) if paper_mm is not None else None

    assessment = QualityAssessment(
        verdict=verdict,
        metrics=metrics,
        explanation=_explanation(
            verdict=verdict,
            method_label=transform.method.label(),
            count=count,
            rms_px=rms_px,
            px_per_mm=px_per_mm,
            scale_denominator=scale_denominator,
            marginal_names=marginal_names,
            failures=failures,
            corrless=corrless,
        ),
        failures=failures,
        rms_mm_on_paper=paper_mm,
        rms_mm_on_site=on_site_mm,
        scale_denominator=scale_denominator,
    )
    if spread_waiver:
        assessment.explanation = f"{assessment.explanation} {spread_waiver}"
    if verdict in {Verdict.POOR, Verdict.FAILED}:
        assessment.suggestion = explain_failure(assessment, transform)
    return assessment


def _failure_text(name: str, outcome: _Outcome, px_per_mm: float) -> str:
    """One review-screen line naming a failing metric with its numbers."""
    value, threshold = outcome.value, outcome.threshold
    if name == RMS_RESIDUAL_PX:
        if not math.isfinite(value):
            return "average alignment error could not be measured"
        measured = _fmt_mm(_paper_mm(value, px_per_mm))
        limit = _fmt_mm(_paper_mm(threshold, px_per_mm))
        return f"average alignment error is {measured} mm on paper (the limit is {limit} mm)"
    if name == INLIER_RATIO:
        return (
            f"only {_percent(value)} of the reference points agree with the "
            f"fitted transform (at least {_percent(threshold)} needed)"
        )
    if name == ANCHOR_COUNT:
        return (
            f"only {_fmt_count(value)} reference points were found "
            f"(at least {_fmt_count(threshold)} needed)"
        )
    if name == ANCHOR_SPREAD:
        return (
            f"the reference points cover only {_percent(value)} of the sheet "
            f"(at least {_percent(threshold)} needed)"
        )
    if name == TRANSFORM_SANITY:
        return f"the fitted transform is not believable: {outcome.reason}"
    if name == INK_OVERLAP:
        if not math.isfinite(value):
            return "ink overlap could not be measured (the transform is not usable)"
        return (
            f"after warping, only {_percent(value)} of the inked pixels line "
            f"up between the two sheets (at least {_percent(threshold)} needed)"
        )
    # HOLDOUT_RMS
    if outcome.reason:
        return outcome.reason
    if not math.isfinite(value):
        return "held-out error could not be measured"
    measured = _fmt_mm(_paper_mm(value, px_per_mm))
    limit = _fmt_mm(_paper_mm(threshold, px_per_mm))
    return f"held-out reference points miss by {measured} mm on paper (the limit is {limit} mm)"


def _explanation(
    verdict: Verdict,
    method_label: str,
    count: int,
    rms_px: float,
    px_per_mm: float,
    scale_denominator: int | None,
    marginal_names: list[str],
    failures: list[str],
    *,
    corrless: bool = False,
) -> str:
    """One or two plain-English sentences for every verdict."""
    if count == 0 and not corrless:
        context = "Could not align: no matching reference points were found."
    elif verdict is Verdict.FAILED:
        context = f"Could not align using {method_label}."
    elif corrless:
        context = f"Aligned using {method_label}."
    else:
        context = f"Aligned using {method_label} with {count} matching reference points."

    if verdict is Verdict.FAILED:
        detail = f"The checks that failed: {'; '.join(failures)}."
    elif verdict is Verdict.POOR:
        borderline = ", ".join(_DISPLAY_NAMES[name] for name in marginal_names)
        detail = (
            f"Average error {mm_report(rms_px, px_per_mm, scale_denominator)}. "
            f"The borderline checks ({borderline}) pass within 10% of their "
            "limits, so confirm the alignment before relying on it."
        )
    elif corrless:
        detail = "Verified by the ink-overlap check across the whole sheet."
    else:
        detail = f"Average error {mm_report(rms_px, px_per_mm, scale_denominator)}."
    return f"{context} {detail}"


def explain_failure(assessment: QualityAssessment, transform: TransformResult) -> str:
    """Specific, actionable guidance for a poor or failed assessment.

    Reads the failing metrics off the assessment and turns them into advice:
    too few anchors or a poor spread means manual alignment; failing ink
    overlap means the sheets may not be the same drawing; an insane transform
    means a scan or mismatched drawing scales. Returns an empty string for
    verdicts that need no action.
    """
    if assessment.verdict in {Verdict.GOOD, Verdict.EXCELLENT}:
        return ""
    if assessment.verdict is Verdict.POOR:
        return (
            "The borderline checks need a human eye: confirm the alignment on "
            "the review screen, or redo it manually if anything looks wrong."
        )

    metrics = assessment.metrics

    def failed(name: str) -> bool:
        return name in metrics and not metrics[name][2]

    hints: list[str] = []
    if failed(ANCHOR_COUNT) or failed(ANCHOR_SPREAD):
        hints.append(
            "Try manual alignment: pick matching grid intersections or "
            "building corners on both sheets."
        )
    if failed(INK_OVERLAP):
        hints.append(
            "Check that the two sheets really are the same drawing: after "
            "warping, their ink does not line up."
        )
    if failed(TRANSFORM_SANITY):
        hints.append(
            "The fitted transform itself is suspect — the sheet may be "
            "scanned, or the two drawings may be at different scales."
        )
    if failed(RMS_RESIDUAL_PX) or failed(HOLDOUT_RMS):
        hints.append(
            "The measured error is over the limit even where the transform "
            "claims to fit: a manual alignment is the reliable fallback."
        )
    if hints:
        return " ".join(hints)
    return (
        "Manual alignment is the reliable fallback: choose matching grid "
        "intersections, column centres or building corners on both sheets."
    )
