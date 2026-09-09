"""Shared vocabulary for Phase 4 alignment.

Every module under ``engine/align/`` and the viewer/API layers speak through
these types so the orchestrator can compose methods that were built
independently: anchors, transform fitting, image methods, refinement, the
quality gate and the orchestrator itself all agree on what a transform, a
fit result and a verdict look like.

Two rules from the Phase 4 plan shape these types:

* **Verdicts are blunt.** ``excellent``/``good`` proceed, ``poor`` needs user
  confirmation, ``failed`` refuses. There is no "probably fine" verdict — the
  gap between "poor" and "failed" is exactly where confident wrong alignments
  hide.
* **Pixels are internal, millimetres are public.** Everything that reaches a
  user is converted to millimetres on paper (and to site millimetres when the
  drawing scale is known) by :func:`quality.mm_on_site` helpers; the dataclasses
  here carry ``px_per_mm`` and the optional scale denominator wherever a
  result crosses the API boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray


class Verdict(StrEnum):
    """Quality verdict of an alignment attempt."""

    EXCELLENT = "excellent"
    GOOD = "good"
    POOR = "poor"
    FAILED = "failed"

    @property
    def proceeds_automatically(self) -> bool:
        return self in {Verdict.EXCELLENT, Verdict.GOOD}


class TransformModel(StrEnum):
    """Degrees of freedom, deliberately ordered from safest to riskiest."""

    SIMILARITY = "similarity"  # translate + rotate + uniform scale (default)
    AFFINE = "affine"  # adds shear / non-uniform scale (scanned sheets only)
    HOMOGRAPHY = "homography"  # perspective; explicit opt-in only


class AlignMethod(StrEnum):
    """Which strategy produced a transform."""

    TEXT_ANCHORS = "text_anchors"
    GRID_BUBBLES = "grid_bubbles"
    PHASE_CORRELATION = "phase_correlation"
    FEATURES = "features"
    SHEET_BORDER = "sheet_border"
    MANUAL = "manual"
    ECC = "ecc_refinement"

    def label(self) -> str:
        labels = {
            AlignMethod.TEXT_ANCHORS: "unique text on the sheet",
            AlignMethod.GRID_BUBBLES: "grid bubbles",
            AlignMethod.PHASE_CORRELATION: "phase correlation",
            AlignMethod.FEATURES: "image features",
            AlignMethod.SHEET_BORDER: "sheet border corners",
            AlignMethod.MANUAL: "manual points",
            AlignMethod.ECC: "refined by ECC",
        }
        return labels[self]


# ── Correspondence material ─────────────────────────────────────────────


@dataclass(slots=True)
class Anchor:
    """One unambiguous reference point on one sheet."""

    #: Normalised text or label (NFKC, upper case, collapsed whitespace).
    text: str
    x: float  # image pixels, content centre
    y: float
    #: Pixel width/height of the text or bubble, for weighting.
    width: float = 1.0
    height: float = 1.0
    #: Where it came from, for explanations.
    source: str = "text"
    confidence: float = 1.0

    @property
    def weight(self) -> float:
        """Longer, larger anchors are more reliable correspondences."""
        return max(1.0, (self.width * self.height) ** 0.5)


@dataclass(slots=True)
class Correspondence:
    """One matched point pair: old sheet -> new sheet, in image pixels."""

    old_x: float
    old_y: float
    new_x: float
    new_y: float
    weight: float = 1.0
    #: The shared label when this came from an anchor/bubble match.
    label: str = ""

    def as_points(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        old = np.array([self.old_x, self.old_y], dtype=float)
        new = np.array([self.new_x, self.new_y], dtype=float)
        return old, new


# ── Transforms ──────────────────────────────────────────────────────────


def identity_matrix() -> NDArray[np.float64]:
    return np.eye(3, dtype=float)


@dataclass(slots=True)
class TransformResult:
    """A fitted 3x3 transform plus everything said about its quality."""

    #: 3x3 matrix mapping old-sheet points onto the new sheet
    #: (``p_new = M @ [x, y, 1]``).
    matrix: NDArray[np.float64] = field(default_factory=identity_matrix)
    model: TransformModel = TransformModel.SIMILARITY
    method: AlignMethod = AlignMethod.MANUAL
    #: Fit statistics (image pixels unless stated).
    inlier_ratio: float = 0.0
    rms_residual_px: float = float("inf")
    residuals_px: list[float] = field(default_factory=list)
    inlier_count: int = 0
    correspondence_count: int = 0
    #: Cross-validation RMS on held-out correspondences (px); NaN = not run.
    holdout_rms_px: float = float("nan")
    #: Peak response for correlation-based methods (0..1-ish).
    confidence: float = 0.0
    #: Decomposed parameters for sanity checks and explanations.
    scale: float = 1.0
    rotation_deg: float = 0.0
    shear: float = 0.0
    determinant: float = 1.0
    #: Translation in pixels.
    tx_px: float = 0.0
    ty_px: float = 0.0
    note: str = ""

    def apply(self, points: NDArray[np.float64]) -> NDArray[np.float64]:
        """Map Nx2 points from old-sheet space to new-sheet space."""
        points = np.asarray(points, dtype=float)
        homogeneous = np.column_stack([points, np.ones(len(points), dtype=float)])
        warped = homogeneous @ self.matrix.T
        return warped[:, :2] / warped[:, 2:3]


# ── The quality gate ─────────────────────────────────────────────────────


@dataclass(slots=True)
class QualityAssessment:
    """The seven metrics plus the verdict and its explanation."""

    verdict: Verdict = Verdict.FAILED
    #: metric name -> (value, threshold, passed)
    metrics: dict[str, tuple[float, float, bool]] = field(default_factory=dict)
    explanation: str = ""
    #: Plain-English list of what failed, for the review screen.
    failures: list[str] = field(default_factory=list)
    suggestion: str = ""
    #: Conversions of the headline error, for display.
    rms_mm_on_paper: float | None = None
    rms_mm_on_site: float | None = None
    scale_denominator: int | None = None


# ── Configuration ────────────────────────────────────────────────────────


@dataclass(slots=True)
class AlignConfig:
    """Every knob the Phase 4 plan exposes, with its plan defaults."""

    dpi: int = 200
    #: Comparison pipeline runs at this fraction first, then refines.
    coarse_scale: float = 0.25
    # Transform fitting.
    ransac_iterations: int = 2000
    ransac_inlier_threshold_px: float = 3.0
    min_inlier_ratio: float = 0.6
    min_anchors: int = 3
    good_anchors: int = 8
    # Quality gate thresholds.
    rms_threshold_px: float = 2.0
    holdout_threshold_px: float = 3.0
    min_anchor_spread: float = 0.25
    min_ink_overlap: float = 0.6
    min_scale: float = 0.2
    max_scale: float = 5.0
    # Metadata prior: computed scale must land within this fraction.
    expected_scale_tolerance: float = 0.15
    # Sheet borders: rotation sanity for CAD sheets.
    rotation_sanity_deg: float = 2.0
    # Grid bubbles: expected bubble size range on paper (mm).
    bubble_min_mm: float = 8.0
    bubble_max_mm: float = 12.0
    bubble_edge_fraction: float = 0.15
    # Per-pair total time budget (seconds); timeout returns best attempt.
    pair_timeout_s: float = 30.0
    ecc_max_iterations: int = 200
    ecc_epsilon: float = 1e-6
    ecc_timeout_s: float = 5.0
    # Rendering.
    render_memory_budget_mb: int = 200
    #: Known drawing scale denominators (old, new) when title blocks gave them.
    old_scale_denominator: int | None = None
    new_scale_denominator: int | None = None
    #: Whether mirroring is allowed (never by default).
    allow_mirror: bool = False
    allow_homography: bool = False


#: A 3x3 matrix mapping new-sheet pixel space onto old-sheet pixel space would
#: be the inverse; all APIs here define M as old -> new, consistently.
