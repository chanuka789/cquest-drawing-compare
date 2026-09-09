"""Task 4.8 - ECC refinement against synthetic line art.

Synthetic pairs are built by warping a rich line-art drawing with a known
3x3 matrix; ``refine_ecc`` is seeded a few pixels off and must pull the
estimate back to ground truth, improve the chamfer RMS residual, honour the
hard wall-clock timeout and never return a worse transform than it was given.
"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np
import pytest

from engine.align.refine import RefineResult, refine_ecc
from engine.align.types import AlignConfig, TransformModel

SHEET_W, SHEET_H = 800, 600


def _drawing(width: int, height: int, seed: int) -> np.ndarray:
    """Rich line art on white: rooms, diagonals, clutter lines."""
    img = np.full((height, width), 255, np.uint8)
    rng = np.random.default_rng(seed)
    mx, my = int(width * 0.16), int(height * 0.16)
    x0, y0, x1e, y1e = mx, my, width - mx, height - my
    for _ in range(5):
        rw = int(rng.integers(width * 0.08, width * 0.20))
        rh = int(rng.integers(height * 0.08, height * 0.20))
        rx = int(rng.integers(x0, x1e - rw))
        ry = int(rng.integers(y0, y1e - rh))
        cv2.rectangle(img, (rx, ry), (rx + rw, ry + rh), 0, 6)
        inner = 5
        cv2.rectangle(
            img,
            (rx + int(0.06 * rw), ry + int(0.06 * rh)),
            (rx + rw - int(0.06 * rw), ry + rh - int(0.06 * rh)),
            0,
            inner,
        )
    for _ in range(24):
        x1 = int(rng.integers(x0, x0 + 0.6 * (x1e - x0)))
        y1 = int(rng.integers(y0, y0 + 0.6 * (y1e - y0)))
        x2 = int(rng.integers(x0 + 0.4 * (x1e - x0), x1e))
        y2 = int(rng.integers(y0 + 0.4 * (y1e - y0), y1e))
        cv2.line(img, (x1, y1), (x2, y2), 0, 5)
    for _ in range(30):
        if rng.random() < 0.5:
            y = int(rng.integers(y0, y1e))
            cv2.line(img, (x0 + 5, y), (x1e - 5, y), 0, 5)
        else:
            x = int(rng.integers(x0, x1e))
            cv2.line(img, (x, y0 + 5), (x, y1e - 5), 0, 5)
    return img


def _similarity(tx: float, ty: float) -> np.ndarray:
    matrix = np.eye(3, dtype=float)
    matrix[0, 2] = tx
    matrix[1, 2] = ty
    return matrix


def _warp(img: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    height, width = img.shape
    return cv2.warpAffine(
        img,
        matrix[:2].astype(np.float64),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def _point_rms(
    estimate: np.ndarray, truth: np.ndarray, width: int, height: int, n: int = 300
) -> float:
    rng = np.random.default_rng(0)
    points = np.column_stack([rng.uniform(0, width, n), rng.uniform(0, height, n)])
    homogeneous = np.column_stack([points, np.ones(n)])
    est = (homogeneous @ estimate.T)[:, :2]
    ref = (homogeneous @ truth.T)[:, :2]
    return float(np.sqrt(np.mean(np.sum((est - ref) ** 2, axis=1))))


def test_ecc_improves_a_3px_off_seed():
    old = _drawing(SHEET_W, SHEET_H, seed=9)
    truth = _similarity(12.0, 8.0)
    new = _warp(old, truth)
    initial = truth.copy()
    initial[0, 2] -= 3.0  # 3 px off in x
    result = refine_ecc(old, new, initial)
    assert isinstance(result, RefineResult)
    assert result.applied is True
    assert result.iterations >= 1
    assert math.isfinite(result.correlation)
    assert result.rms_after_px < result.rms_before_px
    assert _point_rms(result.matrix, truth, SHEET_W, SHEET_H) <= 1.0


def test_ecc_hard_timeout_returns_input_quickly(monkeypatch):
    def sleeping_ecc(*args, **kwargs):
        time.sleep(30.0)
        return 1.0, args[2].copy()

    monkeypatch.setattr(cv2, "findTransformECC", sleeping_ecc)
    old = _drawing(SHEET_W, SHEET_H, seed=9)
    truth = _similarity(12.0, 8.0)
    new = _warp(old, truth)
    initial = truth.copy()
    initial[0, 2] -= 3.0
    started = time.monotonic()
    result = refine_ecc(old, new, initial, config=AlignConfig(ecc_timeout_s=0.4))
    elapsed = time.monotonic() - started
    assert result.applied is False
    assert np.array_equal(result.matrix, initial)
    assert elapsed < 5.0
    monkeypatch.undo()


def test_never_worse_when_seed_is_already_perfect():
    old = _drawing(SHEET_W, SHEET_H, seed=4)
    truth = _similarity(12.0, 8.0)
    new = _warp(old, truth)
    result = refine_ecc(old, new, truth)
    assert result.applied in (True, False)
    assert result.rms_after_px <= result.rms_before_px + 1e-6


def test_ecc_affine_model():
    old = _drawing(SHEET_W, SHEET_H, seed=13)
    truth = np.eye(3, dtype=float)
    truth[0, 0] = 1.02
    truth[1, 1] = 0.98
    truth[0, 1] = 0.01
    truth[1, 0] = -0.005
    truth[0, 2] = 10.0
    truth[1, 2] = 7.0
    new = _warp(old, truth)
    initial = truth.copy()
    initial[0, 2] -= 3.0
    result = refine_ecc(old, new, initial, model=TransformModel.AFFINE)
    assert result.applied is True
    assert result.rms_after_px < result.rms_before_px
    assert _point_rms(result.matrix, truth, SHEET_W, SHEET_H) <= 2.0


def test_ecc_different_sizes_not_applied():
    old = _drawing(SHEET_W, SHEET_H, seed=9)
    truth = _similarity(12.0, 8.0)
    new = _warp(old, truth)[:-40, :-40]
    initial = truth.copy()
    result = refine_ecc(old, new, initial)
    assert result.applied is False
    assert np.array_equal(result.matrix, initial)


def test_ecc_rejects_unsupported_model():
    old = _drawing(SHEET_W, SHEET_H, seed=9)
    new = old.copy()
    with pytest.raises(ValueError, match="homography"):
        refine_ecc(old, new, np.eye(3), model=TransformModel.HOMOGRAPHY)
