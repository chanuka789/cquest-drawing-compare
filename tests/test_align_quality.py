"""Quality gate tests: Task 4.9, the B6 metrics and verdict ladder.

Everything here is synthetic: tiny hand-built grayscale images and point
correspondences constructed around a known transform, so each metric can be
isolated. The plan's hard requirement lives here too — two genuinely
different drawings must return ``failed`` no matter how plausible the fitted
transform claims to be — as do the mm-conversion checks (10 px at 200 DPI is
1.27 mm on paper, 127 mm on site at 1:100).
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from engine.align.quality import assess, explain_failure, mm_report
from engine.align.transform import decompose, similarity_matrix
from engine.align.types import AlignMethod, Correspondence, TransformModel, TransformResult, Verdict

_WIDTH = 400
_HEIGHT = 300

_METRICS = [
    "rms_residual_px",
    "inlier_ratio",
    "anchor_count",
    "anchor_spread",
    "transform_sanity",
    "ink_overlap",
    "holdout_rms",
]

#: Content of the "main" drawing used where the images should match: three
#: solid rectangles that Otsu will binarise cleanly.
_MAIN_RECTS = [(20, 20, 380, 100), (60, 120, 130, 280), (200, 150, 370, 280)]


def _inked(rects: list[tuple[int, int, int, int]]) -> np.ndarray:
    """A white grayscale canvas with black rectangles, clipped to size."""
    image = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    for x0, y0, x1, y1 in rects:
        image[y0:y1, x0:x1] = 0
    return image


def _grid_points(
    columns: int,
    rows: int,
    spacing_x: float,
    spacing_y: float,
    margin: float = 20.0,
) -> list[tuple[float, float]]:
    """A regular grid of points inside the sheet, old and new identical."""
    return [
        (margin + column * spacing_x, margin + row * spacing_y)
        for row in range(rows)
        for column in range(columns)
    ]


def _correspondences(points: list[tuple[float, float]]) -> list[Correspondence]:
    """Identity correspondences (old point maps to the same new point)."""
    return [
        Correspondence(old_x=float(x), old_y=float(y), new_x=float(x), new_y=float(y))
        for x, y in points
    ]


def _result(
    matrix: np.ndarray,
    *,
    method: AlignMethod = AlignMethod.TEXT_ANCHORS,
    model: TransformModel = TransformModel.SIMILARITY,
    rms_px: float | None = None,
    inlier_ratio: float = 1.0,
) -> TransformResult:
    """A TransformResult with the sanity fields matching its own matrix."""
    decomposed = decompose(matrix)
    return TransformResult(
        matrix=matrix,
        model=model,
        method=method,
        scale=math.sqrt(abs(decomposed.determinant)),
        rotation_deg=decomposed.rotation_deg,
        shear=decomposed.shear,
        determinant=decomposed.determinant,
        # inf exercises the recompute-from-correspondences path.
        rms_residual_px=float("inf") if rms_px is None else float(rms_px),
        inlier_ratio=float(inlier_ratio),
    )


def _main_image() -> np.ndarray:
    return _inked(_MAIN_RECTS)


# ── Passing verdicts ──────────────────────────────────────────────────────


def test_clean_identity_alignment_scores_excellent_on_all_metrics():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    assert list(assessment.metrics) == _METRICS
    assert all(passed for _, _, passed in assessment.metrics.values())
    assert assessment.failures == []
    assert assessment.verdict is Verdict.EXCELLENT
    assert assessment.verdict.proceeds_automatically
    assert assessment.explanation.startswith("Aligned using")
    assert "mm on paper" in assessment.explanation


def test_all_metrics_pass_with_six_anchors_is_good_not_excellent():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(3, 2, 160.0, 220.0, margin=40.0)
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    assert assessment.verdict is Verdict.GOOD
    assert assessment.verdict.proceeds_automatically


def test_excellent_with_explicit_sub_pixel_rms_and_near_perfect_overlap():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(5, 5, 80.0, 60.0)
    old = _main_image()
    new = old.copy()
    new[10, 10] = 0  # one stray dot: overlap just under 1.0
    result = _result(matrix, rms_px=0.4)
    assessment = assess(result, _correspondences(points), old, new)

    value, _, passed = assessment.metrics["ink_overlap"]
    assert passed
    assert value == pytest.approx(1.0, abs=0.001)
    assert assessment.verdict is Verdict.EXCELLENT


# ── Failing metrics, one at a time ────────────────────────────────────────


def test_rms_over_threshold_fails_with_mm_conversions():
    matrix = similarity_matrix(10.0, 0.0, 0.0, 1.0)  # transform claims a 10 px shift
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    assessment = assess(
        _result(matrix),
        _correspondences(points),
        image,
        image,
        scale_denominator=100,
    )

    value, threshold, passed = assessment.metrics["rms_residual_px"]
    assert value == pytest.approx(10.0)
    assert threshold == pytest.approx(2.0)
    assert passed is False
    assert assessment.verdict is Verdict.FAILED
    # 10 px at 200 DPI (px_per_mm 200/25.4) is 1.27 mm on paper = 127 mm at 1:100.
    assert assessment.rms_mm_on_paper == pytest.approx(1.27, abs=1e-6)
    assert assessment.rms_mm_on_site == pytest.approx(127.0, abs=1e-6)
    assert assessment.scale_denominator == 100
    assert "Could not align" in assessment.explanation
    assert "1.27 mm" in assessment.explanation
    assert "0.25 mm" in assessment.explanation  # the 2 px limit, in millimetres


def test_low_inlier_ratio_fails():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    result = _result(matrix, inlier_ratio=0.3)
    assessment = assess(result, _correspondences(points), image, image)

    value, threshold, passed = assessment.metrics["inlier_ratio"]
    assert value == pytest.approx(0.3)
    assert threshold == pytest.approx(0.6)
    assert passed is False
    assert assessment.verdict is Verdict.FAILED
    assert "30%" in " ".join(assessment.failures)


def test_anchors_clustered_in_one_corner_fail_spread():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    # Five consistent points squeezed into the bottom-right 2 % of the sheet.
    points = [
        (352.0, 262.0),
        (384.0, 262.0),
        (384.0, 292.0),
        (352.0, 292.0),
        (368.0, 277.0),
    ]
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    value, threshold, passed = assessment.metrics["anchor_spread"]
    assert value == pytest.approx(960.0 / (_WIDTH * _HEIGHT), abs=1e-4)
    assert threshold == pytest.approx(0.25)
    assert passed is False
    assert assessment.metrics["anchor_count"][2] is True
    assert assessment.verdict is Verdict.FAILED
    assert "cover only" in " ".join(assessment.failures)


def test_mirror_matrix_is_rejected_by_transform_sanity():
    matrix = np.asarray([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=float)
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    value, threshold, passed = assessment.metrics["transform_sanity"]
    assert value == 0.0
    assert threshold == 1.0
    assert passed is False
    assert assessment.verdict is Verdict.FAILED
    assert "mirror" in assessment.explanation


def test_shifted_images_fail_ink_overlap():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(5, 5, 80.0, 60.0)
    old = _inked([(40, 40, 360, 260)])
    new = _inked([(240, 40, 560, 260)])  # the same rect, shifted 200 px
    assessment = assess(_result(matrix), _correspondences(points), old, new)

    value, threshold, passed = assessment.metrics["ink_overlap"]
    assert threshold == pytest.approx(0.6)
    assert passed is False
    assert value == pytest.approx(1.0 / 3.0, abs=0.02)
    assert assessment.verdict is Verdict.FAILED
    assert "line up" in " ".join(assessment.failures)


def test_genuinely_different_drawings_cannot_pass():
    """The plan's hard requirement: the impossible pair must return failed."""
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)  # a plausible-looking claim
    points = _grid_points(5, 5, 80.0, 60.0)
    # Vertical bars on the old sheet, horizontal bars on the new: any rigid
    # overlap is well under half, so no believable transform rescues this.
    old = _inked([(30, 10, 80, 290), (170, 10, 220, 290), (310, 10, 360, 290)])
    new = _inked([(10, 40, 390, 90), (10, 140, 390, 190), (10, 240, 390, 290)])
    assessment = assess(_result(matrix), _correspondences(points), old, new)

    value, _, passed = assessment.metrics["ink_overlap"]
    assert passed is False
    # The raw 9-crossing overlap (22500/76500) rises slightly because both
    # masks are dilated by the one-pixel tolerance; it stays far below the
    # 0.6 threshold, so different drawings still fail loudly.
    assert value == pytest.approx(0.3071, abs=0.01)
    assert assessment.verdict is Verdict.FAILED
    assert "same drawing" in assessment.suggestion


# ── Verdict edges ─────────────────────────────────────────────────────────


def test_two_metrics_within_ten_percent_of_failing_verdict_poor():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    result = _result(matrix, rms_px=1.95, inlier_ratio=0.63)
    assessment = assess(result, _correspondences(points), image, image)

    assert assessment.metrics["rms_residual_px"][2] is True
    assert assessment.metrics["inlier_ratio"][2] is True
    assert assessment.failures == []
    assert assessment.verdict is Verdict.POOR
    assert not assessment.verdict.proceeds_automatically
    assert "borderline" in assessment.explanation


def test_zero_correspondences_never_align():
    image = _main_image()
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    assessment = assess(_result(matrix), [], image, image)

    assert assessment.metrics["anchor_count"][2] is False
    assert assessment.verdict is Verdict.FAILED
    assert "no matching reference points" in assessment.explanation
    assert "manual" in assessment.suggestion


def test_non_finite_matrix_never_align():
    matrix = np.full((3, 3), np.nan)
    points = _grid_points(5, 5, 80.0, 60.0)
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    assert assessment.metrics["transform_sanity"][2] is False
    assert assessment.verdict is Verdict.FAILED
    assert "NaN" in assessment.explanation


def test_too_few_anchors_fail_holdout_by_rule_not_by_crash():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(2, 2, 100.0, 100.0)  # 4 anchors: < min_anchors + 2
    image = _main_image()
    assessment = assess(_result(matrix), _correspondences(points), image, image)

    value, _, passed = assessment.metrics["holdout_rms"]
    assert math.isnan(value)
    assert passed is False
    assert "too few anchors to cross-validate" in " ".join(assessment.failures)
    assert assessment.verdict is Verdict.FAILED


# ── Suggestion and mm helpers ─────────────────────────────────────────────


def test_explain_failure_guides_the_user_and_is_safe_when_good():
    matrix = similarity_matrix(0.0, 0.0, 0.0, 1.0)
    points = _grid_points(3, 2, 160.0, 220.0, margin=40.0)
    image = _main_image()
    result = _result(matrix)
    good = assess(result, _correspondences(points), image, image)
    assert good.verdict is Verdict.GOOD
    assert explain_failure(good, result) == ""

    failed = assess(result, [], image, image)
    assert failed.verdict is Verdict.FAILED
    suggestion = explain_failure(failed, result)
    assert "Try manual alignment" in suggestion
    assert failed.suggestion == suggestion


def test_mm_report_states_paper_and_site_millimetres():
    px_per_mm = 200.0 / 25.4
    assert mm_report(10.0, px_per_mm, 100) == ("1.27 mm on paper, which is 127 mm on site at 1:100")
    assert mm_report(10.0, px_per_mm, None) == "1.27 mm on paper"


# ── Ink tolerance and content-scale comparison ────────────────────────────


def _thin_line_sheet() -> np.ndarray:
    """A line-art sheet: 1 px frame, 1 px grid lines and 12x9 'text' marks.

    Strokes are one pixel wide — the sub-pixel raster phase regime the ink
    tolerance exists for.
    """
    image = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (_WIDTH - 21, _HEIGHT - 21), 0, 1)
    for x in range(40, _WIDTH - 40, 32):
        cv2.line(image, (x, 20), (x, _HEIGHT - 21), 0, 1)
    for y in range(40, _HEIGHT - 40, 32):
        cv2.line(image, (20, y), (_WIDTH - 21, y), 0, 1)
    for x0, y0 in ((60, 40), (290, 150), (180, 240)):
        image[y0 : y0 + 9, x0 : x0 + 12] = 0
    return image


def _raw_ink_overlap(old: np.ndarray, new: np.ndarray, matrix3x3: np.ndarray) -> float:
    """The tolerance-free Jaccard (pre-dilation semantics), for comparisons."""
    binary = cv2.threshold(old, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1] == 0
    new_mask = cv2.threshold(new, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1] == 0
    warped = (
        cv2.warpAffine(
            binary.astype(np.uint8) * 255,
            np.asarray(matrix3x3, dtype=float)[:2],
            (new.shape[1], new.shape[0]),
            flags=cv2.INTER_NEAREST,
        )
        > 0
    )
    intersection = int(np.count_nonzero(warped & new_mask))
    union = int(np.count_nonzero(warped | new_mask))
    return intersection / union if union else 0.0


def test_thin_line_sheet_passes_at_an_unlucky_subpixel_phase():
    """A perfect fit on 1 px strokes must pass the ink check.

    The new sheet is the old one re-rendered at a fractional offset
    (0.4, 0.3) px. Otsu binarisation then places most thin strokes one pixel
    away from where the warped old stroke lands, so the exact (tolerance-
    free) Jaccard collapses below the bar; the one-pixel dilation on both
    masks is what lets metric 6 ask "does it look aligned?" rather than "do
    the two rasters' phases agree?".
    """
    old = _thin_line_sheet()
    new = cv2.warpAffine(
        old,
        np.asarray([[1.0, 0.0, 0.4], [0.0, 1.0, 0.3]], dtype=np.float32),
        (_WIDTH, _HEIGHT),
        flags=cv2.INTER_LINEAR,
    )
    matrix = similarity_matrix(0.4, 0.3, 0.0, 1.0)
    result = _result(matrix, method=AlignMethod.PHASE_CORRELATION)
    assessment = assess(result, [], old, new)

    value, _, passed = assessment.metrics["ink_overlap"]
    assert passed
    assert value >= 0.6
    assert assessment.verdict.proceeds_automatically
    # The test really sits in the phase-fragile regime: without the tolerance
    # the exact overlap is below the gate's bar.
    assert _raw_ink_overlap(old, new, matrix) < 0.6


def test_scale_tolerant_ink_passes_a_doubled_content_sheet():
    """A 2.0x content re-issue is verified at the old sheet's content scale.

    Models the rescaled fixture: the same canvas, the old content in the
    centre, the new content doubled about the centre so it fills the canvas.
    When a similarity fit magnifies content by more than 5% the ink
    comparison resizes the new sheet onto the old canvas (INTER_AREA) and
    warps the old sheet by the scale-normalised matrix, so both sheets are
    sampled at the same density; with the correct matrix the overlap must
    clear the bar.
    """
    size = 300
    content = np.full((size // 2, size // 2), 255, dtype=np.uint8)
    cv2.rectangle(content, (10, 10), (size // 2 - 11, size // 2 - 11), 0, 1)
    for x in range(20, size // 2 - 20, 24):
        cv2.line(content, (x, 10), (x, size // 2 - 11), 0, 1)
    for y in range(20, size // 2 - 20, 24):
        cv2.line(content, (10, y), (size // 2 - 11, y), 0, 1)
    content[size // 4 : size // 4 + 12, size // 4 : size // 4 + 16] = 0

    old = np.full((size, size), 255, dtype=np.uint8)
    old[size // 4 : 3 * size // 4, size // 4 : 3 * size // 4] = content
    new = cv2.resize(content, (size, size), interpolation=cv2.INTER_LINEAR)

    matrix = np.eye(3)
    matrix[0, 0] = matrix[1, 1] = 2.0
    matrix[0, 2] = matrix[1, 2] = -size / 2.0
    result = _result(matrix, method=AlignMethod.PHASE_CORRELATION)
    assessment = assess(result, [], old, new)

    value, _, passed = assessment.metrics["ink_overlap"]
    assert passed
    assert value >= 0.6
    assert assessment.verdict.proceeds_automatically


def test_anchor_spread_waived_for_genuine_content_scale_change():
    """A partial-content sheet enlarged 2x is not refused on anchor spread.

    The old content occupies only the centre 80 x 120 px of a 400 x 300
    sheet, so its anchors can never reach the 0.25 spread rule — exactly the
    situation of a small drawing re-issued at a larger scale. With every
    other metric passing (notably the content-scale ink check), the spread
    refusal is waived and the verdict caps at good.
    """
    old = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    old[90:210, 160:240] = 0  # one centred content block
    new = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    new[30:270, 120:280] = 0  # the same block doubled about the page centre

    matrix = np.eye(3)
    matrix[0, 0] = matrix[1, 1] = 2.0
    matrix[0, 2], matrix[1, 2] = -200.0, -150.0
    points = [
        (float(x), float(y)) for y in (110.0, 150.0, 190.0) for x in (170.0, 190.0, 210.0, 230.0)
    ]
    correspondences = [
        Correspondence(old_x=x, old_y=y, new_x=2.0 * x - 200.0, new_y=2.0 * y - 150.0)
        for x, y in points
    ]
    assessment = assess(_result(matrix), correspondences, old, new)

    spread_metric = assessment.metrics["anchor_spread"]
    assert spread_metric[2]  # waived: recorded as passed
    assert assessment.verdict is Verdict.GOOD  # capped: a waiver is not excellent
    assert assessment.verdict.proceeds_automatically
    assert "waived" in assessment.explanation


def test_content_scale_change_does_not_waive_a_visually_wrong_fit():
    """The spread waiver never fires when the ink check fails."""
    old = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    old[90:210, 160:240] = 0
    # A "doubled" sheet whose ink does not line up with the old content.
    new = np.full((_HEIGHT, _WIDTH), 255, dtype=np.uint8)
    new[30:270, 40:200] = 0

    matrix = np.eye(3)
    matrix[0, 0] = matrix[1, 1] = 2.0
    matrix[0, 2], matrix[1, 2] = -200.0, -150.0
    correspondences = [
        Correspondence(old_x=float(x), old_y=float(y), new_x=2.0 * x - 200.0, new_y=2.0 * y - 150.0)
        for y in (110.0, 150.0, 190.0)
        for x in (170.0, 190.0, 210.0, 230.0)
    ]
    assessment = assess(_result(matrix), correspondences, old, new)

    assert assessment.verdict is Verdict.FAILED
    assert "waived" not in assessment.explanation
