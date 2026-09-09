"""Transform fitting, decomposition and validation tests: Task 4.6.

Everything here is synthetic: correspondences are generated from a *known*
ground-truth transform, so the fitted answer can be compared exactly. The
plan's headline cases are covered — exact recovery of a similarity, affine
recovery with shear, RANSAC surviving 40% wild correspondences, hold-out
cross-validation inflated by an outlier region, and the transform sanity
gate refusing reflections, wild scales, and off-axis rotation for CAD
sheets.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from engine.align.transform import (
    affine_matrix_from_2x2,
    cross_validate,
    decompose,
    fit_transform,
    similarity_matrix,
    validate_transform,
)
from engine.align.types import (
    AlignConfig,
    AlignMethod,
    Correspondence,
    TransformModel,
    TransformResult,
)


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Map an (N, 2) array of old points to new points under *matrix*."""
    return points @ matrix[:2, :2].T + matrix[:2, 2]


def _correspondences(old: np.ndarray, new: np.ndarray) -> list[Correspondence]:
    return [
        Correspondence(old_x=float(x1), old_y=float(y1), new_x=float(x2), new_y=float(y2))
        for (x1, y1), (x2, y2) in zip(old, new, strict=True)
    ]


def _grid(width: int, height: int, spacing: float) -> np.ndarray:
    return np.asarray(
        [[x * spacing + 100.0, y * spacing + 100.0] for y in range(height) for x in range(width)],
        dtype=float,
    )


def _result_from_matrix(
    matrix: np.ndarray,
    method: AlignMethod = AlignMethod.TEXT_ANCHORS,
) -> TransformResult:
    """A TransformResult whose sanity fields mirror what fitting fills in."""
    decomposed = decompose(matrix)
    return TransformResult(
        matrix=matrix,
        model=TransformModel.SIMILARITY,
        method=method,
        scale=math.sqrt(abs(decomposed.determinant)),
        rotation_deg=decomposed.rotation_deg,
        shear=decomposed.shear,
        determinant=decomposed.determinant,
        tx_px=decomposed.tx,
        ty_px=decomposed.ty,
    )


# ── Decomposition and builders ───────────────────────────────────────────


def test_decompose_round_trips_similarity():
    matrix = similarity_matrix(tx=30.0, ty=-12.0, rotation_deg=7.0, scale=1.25)
    decomposed = decompose(matrix)
    assert decomposed.tx == pytest.approx(30.0, abs=1e-9)
    assert decomposed.ty == pytest.approx(-12.0, abs=1e-9)
    assert decomposed.rotation_deg == pytest.approx(7.0, abs=1e-9)
    assert decomposed.scale_x == pytest.approx(1.25, abs=1e-9)
    assert decomposed.scale_y == pytest.approx(1.25, abs=1e-9)
    assert decomposed.shear == pytest.approx(0.0, abs=1e-12)
    assert decomposed.determinant == pytest.approx(1.25**2, abs=1e-9)


def test_decompose_reads_affine_scale_and_shear():
    matrix = affine_matrix_from_2x2([[1.02, 0.05], [0.0, 0.97]], tx=5.0, ty=-7.0)
    decomposed = decompose(matrix)
    assert decomposed.tx == pytest.approx(5.0)
    assert decomposed.ty == pytest.approx(-7.0)
    assert decomposed.scale_x == pytest.approx(1.02, abs=1e-9)
    assert decomposed.scale_y == pytest.approx(0.97, abs=1e-9)
    assert decomposed.shear == pytest.approx(0.05, abs=1e-9)
    assert decomposed.rotation_deg == pytest.approx(0.0, abs=1e-9)
    assert decomposed.determinant == pytest.approx(1.02 * 0.97, abs=1e-9)


def test_decompose_sees_a_mirror():
    matrix = affine_matrix_from_2x2([[-1.2, 0.0], [0.0, 1.2]], tx=0.0, ty=0.0)
    decomposed = decompose(matrix)
    assert decomposed.determinant == pytest.approx(-1.44)
    assert decomposed.scale_x == pytest.approx(1.2)


# ── Least-squares fitting ───────────────────────────────────────────────


def test_ls_recovers_known_similarity_exactly():
    truth = similarity_matrix(tx=30.0, ty=-12.0, rotation_deg=7.0, scale=1.25)
    old = _grid(width=3, height=3, spacing=300.0)
    new = _apply(truth, old)
    result = fit_transform(
        _correspondences(old, new),
        method="ls",
        method_name=AlignMethod.TEXT_ANCHORS,
    )
    assert result.rms_residual_px < 1e-6
    assert np.max(np.abs(result.matrix - truth)) < 1e-6
    assert result.inlier_ratio == 1.0
    assert result.inlier_count == len(old)
    assert result.correspondence_count == len(old)
    assert result.scale == pytest.approx(1.25, abs=1e-9)
    assert result.rotation_deg == pytest.approx(7.0, abs=1e-9)
    assert result.shear == pytest.approx(0.0, abs=1e-9)
    assert result.tx_px == pytest.approx(30.0, abs=1e-9)
    assert result.ty_px == pytest.approx(-12.0, abs=1e-9)
    assert result.method == AlignMethod.TEXT_ANCHORS
    assert len(result.residuals_px) == len(old)


def test_affine_ls_recovers_shear_transform():
    truth = affine_matrix_from_2x2([[1.02, 0.05], [0.0, 0.97]], tx=30.0, ty=-12.0)
    old = _grid(width=3, height=4, spacing=250.0)
    new = _apply(truth, old)
    result = fit_transform(_correspondences(old, new), model=TransformModel.AFFINE, method="ls")
    assert result.rms_residual_px < 1e-6
    assert np.max(np.abs(result.matrix - truth)) < 1e-6
    assert result.determinant == pytest.approx(1.02 * 0.97, abs=1e-9)


def test_ls_ignores_zero_weight_correspondences():
    """Weighted least squares must let a caller silence a known-bad point."""
    truth = similarity_matrix(tx=10.0, ty=5.0, rotation_deg=0.0, scale=1.0)
    old = _grid(width=2, height=2, spacing=700.0)
    correspondences = _correspondences(old, _apply(truth, old))
    # Two wild correspondences with zero weight: they must not move the fit.
    correspondences.append(
        Correspondence(old_x=0.0, old_y=0.0, new_x=2000.0, new_y=2000.0, weight=0.0)
    )
    correspondences.append(
        Correspondence(old_x=0.0, old_y=0.0, new_x=-1500.0, new_y=1200.0, weight=0.0)
    )
    result = fit_transform(correspondences, method="ls")
    assert np.max(np.abs(result.matrix - truth)) < 1e-6


def test_fit_transform_refuses_too_few_correspondences():
    old = np.asarray([[100.0, 100.0]], dtype=float)
    result = fit_transform(_correspondences(old, old + 5.0), method="ls")
    assert not np.isfinite(result.rms_residual_px)
    assert result.inlier_ratio == 0.0
    assert "at least 2 correspondences" in result.note


def test_fit_transform_refuses_homography():
    old = _grid(width=3, height=3, spacing=300.0)
    with pytest.raises(ValueError, match="homography"):
        fit_transform(_correspondences(old, old + 1.0), model=TransformModel.HOMOGRAPHY)


# ── RANSAC ──────────────────────────────────────────────────────────────


def test_ransac_survives_forty_percent_wild_correspondences():
    truth = similarity_matrix(tx=15.0, ty=8.0, rotation_deg=-3.0, scale=1.1)
    rng = np.random.default_rng(123)
    inlier_old = rng.uniform(200.0, 2500.0, size=(60, 2))
    inlier_new = _apply(truth, inlier_old)
    wild_old = rng.uniform(200.0, 2500.0, size=(40, 2))
    angles = rng.uniform(0.0, 2.0 * math.pi, size=40)
    magnitudes = rng.uniform(300.0, 900.0, size=40)
    wild_new = wild_old + magnitudes[:, None] * np.column_stack([np.cos(angles), np.sin(angles)])
    old = np.concatenate([inlier_old, wild_old])
    new = np.concatenate([inlier_new, wild_new])
    result = fit_transform(_correspondences(old, new), method="ransac")

    assert result.rms_residual_px < 0.5
    assert result.inlier_count == 60
    assert result.inlier_ratio == pytest.approx(0.6)
    assert np.max(np.abs(result.matrix - truth)) < 0.5
    assert result.scale == pytest.approx(1.1, abs=1e-3)
    assert result.rotation_deg == pytest.approx(-3.0, abs=1e-3)


def test_ransac_low_consensus_is_returned_with_a_note():
    """Below the minimum inlier ratio the best fit still comes back, flagged."""
    truth = similarity_matrix(tx=0.0, ty=0.0, rotation_deg=0.0, scale=1.0)
    inlier_old = _grid(width=3, height=2, spacing=400.0)
    inlier_new = _apply(truth, inlier_old)
    rng = np.random.default_rng(5)
    wild_old = rng.uniform(100.0, 2000.0, size=(len(inlier_old) * 4, 2))
    wild_new = wild_old + rng.uniform(400.0, 900.0, size=(len(inlier_old) * 4, 2))
    old = np.concatenate([inlier_old, wild_old])
    new = np.concatenate([inlier_new, wild_new])
    result = fit_transform(
        _correspondences(old, new),
        config=AlignConfig(min_inlier_ratio=0.9),
        method="ransac",
    )
    assert result.inlier_ratio < 0.9
    assert "below the minimum" in result.note


# ── Cross-validation ────────────────────────────────────────────────────


def test_cross_validate_near_zero_on_clean_correspondences():
    truth = similarity_matrix(tx=40.0, ty=-25.0, rotation_deg=12.0, scale=1.05)
    old = _grid(width=6, height=4, spacing=320.0)
    correspondences = _correspondences(old, _apply(truth, old))
    mean, std = cross_validate(correspondences)
    assert mean < 0.001
    assert std < 0.001


def test_cross_validate_inflated_by_an_outlier_region():
    truth = similarity_matrix(tx=10.0, ty=-6.0, rotation_deg=4.0, scale=1.0)
    old = _grid(width=5, height=4, spacing=350.0)
    correspondences = _correspondences(old, _apply(truth, old))
    clean_mean, _ = cross_validate(correspondences)

    rng = np.random.default_rng(11)
    wild_old = rng.uniform(100.0, 1900.0, size=(5, 2))
    wild_new = wild_old + rng.uniform(300.0, 800.0, size=(5, 2))
    polluted = correspondences + _correspondences(wild_old, wild_new)
    dirty_mean, _ = cross_validate(polluted)

    assert clean_mean < 0.001
    assert dirty_mean > 20.0
    assert dirty_mean > clean_mean * 1000.0


# ── Validation ──────────────────────────────────────────────────────────


def test_validate_rejects_a_mirror_unless_enabled():
    mirror = _result_from_matrix(affine_matrix_from_2x2([[-1.2, 0.0], [0.0, 1.2]], 0.0, 0.0))
    assert mirror.determinant < 0.0
    problems = validate_transform(mirror)
    assert len(problems) == 1
    assert "mirror" in problems[0]
    assert validate_transform(mirror, allow_mirror=True) == []


def test_validate_rejects_wild_scale():
    huge = _result_from_matrix(similarity_matrix(0.0, 0.0, 0.0, 7.0))
    problems = validate_transform(huge)
    assert any("scale" in problem for problem in problems)


def test_validate_accepts_expected_scale_within_tolerance():
    plausible = _result_from_matrix(similarity_matrix(10.0, 10.0, 0.0, math.sqrt(2.0)))
    # 1.414 vs the metadata prior 1.35 is a 4.7% mismatch — inside 15%.
    assert validate_transform(plausible, expected_scale=1.35) == []
    # 3.0 vs the same prior is a 122% mismatch — rejected.
    wrong = _result_from_matrix(similarity_matrix(10.0, 10.0, 0.0, 3.0))
    problems = validate_transform(wrong, expected_scale=1.35)
    assert any("does not match the drawing scales" in problem for problem in problems)


def test_validate_rotation_sanity_for_cad_sheets():
    tilted = _result_from_matrix(similarity_matrix(10.0, 10.0, 20.0, 1.0))
    problems = validate_transform(tilted)
    assert any("rotation" in problem for problem in problems)
    # Scanned sheets may arrive skewed.
    assert validate_transform(tilted, is_scanned=True) == []
    # Manual points are the user's own truth.
    manual = _result_from_matrix(
        similarity_matrix(10.0, 10.0, 20.0, 1.0), method=AlignMethod.MANUAL
    )
    assert validate_transform(manual) == []


def test_validate_accepts_quarter_turn_rotation():
    rotated = _result_from_matrix(similarity_matrix(0.0, 0.0, 90.0, 1.0))
    assert validate_transform(rotated) == []
    rotated_270 = _result_from_matrix(similarity_matrix(0.0, 0.0, -90.0, 1.0))
    assert validate_transform(rotated_270) == []


def test_validate_rejects_shear_on_a_cad_sheet():
    sheared = _result_from_matrix(
        affine_matrix_from_2x2([[1.0, 0.05], [0.0, 1.0]], 0.0, 0.0),
        method=AlignMethod.TEXT_ANCHORS,
    )
    problems = validate_transform(sheared)
    assert any("shear" in problem for problem in problems)
    assert validate_transform(sheared, is_scanned=True) == []


def test_validate_accepts_sheet_border_method_rotation():
    border = _result_from_matrix(
        similarity_matrix(0.0, 0.0, 45.0, 1.0), method=AlignMethod.SHEET_BORDER
    )
    assert validate_transform(border) == []
