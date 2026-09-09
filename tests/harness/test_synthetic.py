"""Unit tests for the Task 4.0 ground-truth harness.

Fast tests only, deliberately unmarked: the full alignment benchmark is
opt-in via the ``benchmark`` pytest marker and lives in later tasks. Where a
test needs "the expected matrix", it recomposes the spec independently
(centre translation x rotation x scale x canvas scale as plain 3x3 products,
then numpy-inverts) rather than trusting the closed-form code under test, and
it re-warps the old image with that independently derived matrix and
requires pixel agreement with the pair the harness built.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from tests.fixture_builder import SheetSpec, build_pdf
from tests.harness.benchmark import CSV_COLUMNS, BenchCase, default_cases, run_benchmark
from tests.harness.synthetic import (
    DegradationSpec,
    TransformSpec,
    generate_pair,
    similarity_parameters,
)

DPI = 100
_INK = 200  # pixel values below this count as ink


@pytest.fixture(scope="module")
def source_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One A1 fixture sheet, shared by every test in this module."""
    return build_pdf(tmp_path_factory.mktemp("harness") / "a1.pdf", [SheetSpec()])


def _expected_matrix(
    spec: TransformSpec,
    px_per_mm: float,
    old_shape: tuple[int, int],
    new_shape: tuple[int, int],
) -> NDArray[np.float64]:
    """Independently recompose the ground-truth matrix from the spec.

    p_new = K @ T(t_mm) @ T(c) @ S(scale) @ R(rotation) @ T(-c), where every
    step is a plain 3x3 matrix product, the rotation is the standard
    [[cos, -sin], [sin, cos]] in pixel coordinates, and the canvas resize
    K = diag(k, k, 1) scales uniformly by the width ratio.
    """
    old_h, old_w = old_shape
    new_h, new_w = new_shape
    del new_h
    cosine = np.cos(np.radians(spec.rotation_deg))
    sine = np.sin(np.radians(spec.rotation_deg))
    rotation = np.array([[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]])
    scale = np.diag([spec.scale, spec.scale, 1.0])
    centre_x = old_w / 2.0
    centre_y = old_h / 2.0
    to_centre = np.array([[1.0, 0.0, centre_x], [0.0, 1.0, centre_y], [0.0, 0.0, 1.0]])
    from_centre = np.array([[1.0, 0.0, -centre_x], [0.0, 1.0, -centre_y], [0.0, 0.0, 1.0]])
    translate = np.array(
        [
            [1.0, 0.0, spec.translation_mm[0] * px_per_mm],
            [0.0, 1.0, spec.translation_mm[1] * px_per_mm],
            [0.0, 0.0, 1.0],
        ]
    )
    canvas_scale = new_w / old_w
    canvas = np.diag([canvas_scale, canvas_scale, 1.0])
    return canvas @ translate @ to_centre @ scale @ rotation @ from_centre


def _warp_with_expected(
    old_image: NDArray[np.uint8], expected: NDArray[np.float64], new_shape: tuple[int, int]
) -> NDArray[np.uint8]:
    """Warp with the independently built matrix (OpenCV applies it forward)."""
    return cv2.warpAffine(
        old_image,
        expected[:2],
        (new_shape[1], new_shape[0]),
        flags=cv2.INTER_LINEAR,
        borderValue=255,
    )


def _assert_pixels_match(actual: NDArray[np.uint8], expected: NDArray[np.uint8]) -> None:
    diff = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    assert diff.mean() < 0.01, "harness warp disagrees with the independent matrix"
    assert diff.max() <= 2, "harness warp disagrees with the independent matrix"


def test_identity_pipeline_new_equals_old(source_pdf: Path) -> None:
    pair = generate_pair(source_pdf, transform=TransformSpec(), dpi=DPI)
    assert pair.old_image.shape == pair.new_image.shape
    assert np.array_equal(pair.old_image, pair.new_image)
    assert pair.error_of(pair.gt_matrix) < 1e-12


def test_pure_translation_recovers_exact_matrix(source_pdf: Path) -> None:
    spec = TransformSpec(translation_mm=(25.4, 12.7))
    pair = generate_pair(source_pdf, transform=spec, dpi=DPI)
    expected = _expected_matrix(spec, pair.px_per_mm, pair.old_image.shape, pair.new_image.shape)
    assert pair.error_of(expected) < 1e-6
    tx_px, ty_px, rotation_deg, scale = similarity_parameters(pair.gt_matrix)
    assert abs(tx_px - 100.0) < 1e-6  # 25.4 mm at 100 dpi
    assert abs(ty_px - 50.0) < 1e-6
    assert abs(rotation_deg) < 1e-9
    assert abs(scale - 1.0) < 1e-12
    # Content really moved down-right by (100, 50) px: the bands it vacated
    # at the top and left edges must be pure white.
    assert (pair.new_image[:50, :] == 255).all()
    assert (pair.new_image[:, :100] == 255).all()


def test_rotation_90_about_old_centre(source_pdf: Path) -> None:
    spec = TransformSpec(rotation_deg=90.0)
    pair = generate_pair(source_pdf, transform=spec, dpi=DPI)
    expected = _expected_matrix(spec, pair.px_per_mm, pair.old_image.shape, pair.new_image.shape)
    assert pair.error_of(expected) < 1e-6
    _, _, rotation_deg, scale = similarity_parameters(pair.gt_matrix)
    assert abs(rotation_deg - 90.0) < 1e-6
    assert abs(scale - 1.0) < 1e-9
    # Rotation is about the old centre: the centre must be a fixed point.
    old_h, old_w = pair.old_image.shape
    centre = np.array([old_w / 2.0, old_h / 2.0, 1.0])
    mapped = pair.gt_matrix @ centre
    assert abs(mapped[0] - centre[0]) < 1e-6
    assert abs(mapped[1] - centre[1]) < 1e-6
    _assert_pixels_match(
        pair.new_image, _warp_with_expected(pair.old_image, expected, pair.new_image.shape)
    )


def test_scale_1414(source_pdf: Path) -> None:
    spec = TransformSpec(scale=1.414)
    pair = generate_pair(source_pdf, transform=spec, dpi=DPI)
    expected = _expected_matrix(spec, pair.px_per_mm, pair.old_image.shape, pair.new_image.shape)
    assert pair.error_of(expected) < 1e-6
    _, _, rotation_deg, scale = similarity_parameters(pair.gt_matrix)
    assert abs(rotation_deg) < 1e-9
    assert abs(scale - 1.414) < 1e-9
    _assert_pixels_match(
        pair.new_image, _warp_with_expected(pair.old_image, expected, pair.new_image.shape)
    )


def test_combined_rotation_scale_translation(source_pdf: Path) -> None:
    spec = TransformSpec(translation_mm=(8.0, -4.0), rotation_deg=20.0, scale=1.25)
    pair = generate_pair(source_pdf, transform=spec, dpi=DPI)
    expected = _expected_matrix(spec, pair.px_per_mm, pair.old_image.shape, pair.new_image.shape)
    assert pair.error_of(expected) < 1e-6
    _, _, rotation_deg, scale = similarity_parameters(pair.gt_matrix)
    assert abs(rotation_deg - 20.0) < 1e-6
    assert abs(scale - 1.25) < 1e-9
    _assert_pixels_match(
        pair.new_image, _warp_with_expected(pair.old_image, expected, pair.new_image.shape)
    )


def test_canvas_resize_a1_to_a2_maps_known_point(source_pdf: Path) -> None:
    spec = TransformSpec(page_size_change="a2")
    pair = generate_pair(source_pdf, transform=spec, dpi=DPI)
    # A2 landscape is 594 x 420 mm: 2339 x 1654 px at 100 dpi.
    assert pair.new_image.shape == (1654, 2339)
    expected = _expected_matrix(spec, pair.px_per_mm, pair.old_image.shape, pair.new_image.shape)
    assert pair.error_of(expected) < 1e-6
    _, _, rotation_deg, scale = similarity_parameters(pair.gt_matrix)
    k = pair.new_image.shape[1] / pair.old_image.shape[1]
    assert abs(rotation_deg) < 1e-9
    assert abs(scale - k) < 1e-12
    # Content check: a dark point on the old border ring must map onto dark
    # pixels in the new image at the matrix-predicted location.
    old = pair.old_image
    mid_row = old.shape[0] // 2
    dark_columns = np.nonzero(old[mid_row] < _INK)[0]
    assert dark_columns.size > 0
    point = np.array([float(dark_columns[0]), float(mid_row), 1.0])
    mapped = pair.gt_matrix @ point
    new_x = mapped[0] / mapped[2]
    new_y = mapped[1] / mapped[2]
    window = pair.new_image[
        max(0, int(new_y) - 4) : int(new_y) + 5, max(0, int(new_x) - 4) : int(new_x) + 5
    ]
    assert window.min() < _INK
    _assert_pixels_match(
        pair.new_image, _warp_with_expected(pair.old_image, expected, pair.new_image.shape)
    )


def test_error_of_is_zero_for_true_matrix_and_grows_for_shifted(source_pdf: Path) -> None:
    pair = generate_pair(source_pdf, transform=TransformSpec(), dpi=DPI)
    assert pair.error_of(pair.gt_matrix) == 0.0
    shifted = pair.gt_matrix.copy()
    shifted[0, 2] += 3.0
    error = pair.error_of(shifted)
    assert abs(error - 3.0) < 1e-9


def test_similarity_parameters_decomposes_known_matrix() -> None:
    matrix = np.array([[0.0, -2.0, 3.0], [2.0, 0.0, 4.0], [0.0, 0.0, 1.0]], dtype=float)
    tx, ty, rotation_deg, scale = similarity_parameters(matrix)
    assert abs(tx - 3.0) < 1e-12
    assert abs(ty - 4.0) < 1e-12
    assert abs(rotation_deg - 90.0) < 1e-9
    assert abs(scale - 2.0) < 1e-12
    # A 2x3 input (the shape cv2 fitters return) decomposes identically.
    tx2, ty2, rotation2, scale2 = similarity_parameters(matrix[:2])
    assert (tx2, ty2, rotation2, scale2) == pytest.approx((tx, ty, rotation_deg, scale))


_DEGRADATION_CASES: list[tuple[str, DegradationSpec, float]] = [
    ("blur", DegradationSpec(blur_sigma=2.0), 0.02),
    ("noise", DegradationSpec(noise_sigma=5.0), 1.0),
    ("jpeg", DegradationSpec(jpeg_quality=40), 0.005),
    ("thicker-lines", DegradationSpec(line_weight_change=1), 0.02),
    ("thinner-lines", DegradationSpec(line_weight_change=-1), 0.02),
    ("skew", DegradationSpec(skew_deg=1.0), 0.02),
]


@pytest.mark.parametrize(("label", "degradation", "min_mean_diff"), _DEGRADATION_CASES)
def test_degradations_alter_the_new_image(
    source_pdf: Path, label: str, degradation: DegradationSpec, min_mean_diff: float
) -> None:
    pair = generate_pair(source_pdf, transform=TransformSpec(), degradation=degradation, dpi=DPI)
    assert pair.new_image.shape == pair.old_image.shape
    diff = np.abs(pair.new_image.astype(np.int16) - pair.old_image.astype(np.int16))
    assert diff.mean() > min_mean_diff, f"{label} degradation had no visible effect"
    assert diff.max() > 0, f"{label} degradation left the image untouched"


@pytest.mark.parametrize("shape", ["rect", "circle", "line"], ids=["rect", "circle", "line"])
def test_content_change_erases_and_pastes_shape(source_pdf: Path, shape: str) -> None:
    # Covers the fixture's left-hand body text ("GRID A" at ~(497, 700) px),
    # so the erase visibly removes real ink before the shape is pasted.
    region = (250, 500, 700, 400)
    pair = generate_pair(
        source_pdf,
        transform=TransformSpec(),
        degradation=DegradationSpec(content_change=(region, shape)),
        dpi=DPI,
    )
    x, y, w, h = region
    old_block = pair.old_image[y : y + h, x : x + w]
    assert old_block.min() < _INK, "the test region should contain real ink to erase"
    block = pair.new_image[y : y + h, x : x + w]
    assert block.min() < 128, f"{shape} shape was not pasted into the erased region"
    assert block[5, 5] == 255, "erased region corners must be white (content was erased)"


def test_content_change_can_erase_without_a_shape(source_pdf: Path) -> None:
    # A wide vertical band through the left border ring: after erasing, the
    # whole band must be white while the rest of the sheet keeps its ink.
    region = (0, 0, 200, 1_000_000)
    pair = generate_pair(
        source_pdf,
        transform=TransformSpec(),
        degradation=DegradationSpec(content_change=(region, None)),
        dpi=DPI,
    )
    assert (pair.new_image[:, :200] == 255).all(), "erased band must be pure white"
    assert pair.new_image.min() < _INK, "the rest of the sheet still has ink"


class _ExactFakeResult:
    """An alignment result that is exact on the cases it is handed."""

    def __init__(self, matrix: NDArray[np.float64]) -> None:
        self.matrix = matrix
        self.verdict = "good"
        self.method = "fake-exact"


class _ExactFake:
    """An align_fn that returns the identity - exact for identity cases."""

    def __call__(
        self, old_image: NDArray[np.uint8], new_image: NDArray[np.uint8]
    ) -> _ExactFakeResult:
        del old_image, new_image
        return _ExactFakeResult(np.eye(3))


class _WrongFake:
    """An align_fn that confidently returns the identity even when wrong."""

    def __call__(
        self, old_image: NDArray[np.uint8], new_image: NDArray[np.uint8]
    ) -> tuple[NDArray[np.float64], str]:
        del old_image, new_image
        return np.eye(3), "good"


def test_benchmark_exact_fake_has_zero_dangerous_failures(source_pdf: Path, tmp_path: Path) -> None:
    csv_path = tmp_path / "benchmark.csv"
    report = run_benchmark(
        _ExactFake(),
        cases=[BenchCase("identity", TransformSpec(), DegradationSpec())],
        source_pdf=source_pdf,
        dpi=DPI,
        out_csv=csv_path,
    )
    assert len(report.rows) == 1
    assert report.dangerous_failures == []
    row = report.rows[0]
    assert row["verdict"] == "good"
    assert row["method"] == "fake-exact"
    assert row["error_px"] < 1e-9
    assert row["dangerous"] is False
    summary = report.summary()
    assert "0 dangerous failures" in summary
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == CSV_COLUMNS
    assert len(lines) == 2


def test_benchmark_wrong_fake_has_dangerous_failures(source_pdf: Path) -> None:
    report = run_benchmark(
        _WrongFake(),
        cases=[
            BenchCase("shift-100px", TransformSpec(translation_mm=(25.4, 0.0)), DegradationSpec())
        ],
        source_pdf=source_pdf,
        dpi=DPI,
    )
    assert len(report.dangerous_failures) == 1
    row = report.rows[0]
    assert row["case"] == "shift-100px"
    assert row["verdict"] == "good"
    assert row["method"] == "unknown"  # tuple results carry no method
    assert 90.0 < row["error_px"] < 110.0  # ~100 px shift at 100 dpi
    assert row["dangerous"] is True
    assert report.summary().count("1 dangerous failures") == 1


def test_default_cases_smoke_subset_and_full_matrix() -> None:
    smoke = default_cases()
    assert len(smoke) == 4  # 2 shifts x 2 rotations x 1 scale x 1 severity
    assert len({case.name for case in smoke}) == len(smoke)
    assert all(case.degradation == DegradationSpec() for case in smoke)
    assert any(case.transform.rotation_deg == 1.5 for case in smoke)
    full = default_cases(full=True)
    assert len(full) == 225  # 3 x 5 x 5 x 3
    assert len({case.name for case in full}) == len(full)
    assert sorted({case.transform.scale for case in full}) == pytest.approx(
        [0.5, 0.707, 1.0, 1.414, 2.0]
    )
    assert {case.transform.rotation_deg for case in full} == {0.0, 1.5, 45.0, 90.0, 180.0}
    assert any(case.degradation.jpeg_quality == 60 for case in full)
    assert any(case.degradation.line_weight_change == 1 for case in full)
    assert any(case.degradation.noise_sigma == 8.0 for case in full)
