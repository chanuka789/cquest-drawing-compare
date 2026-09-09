"""Fitting transforms from correspondences: Task 4.6.

Correspondences give point pairs (old sheet -> new sheet); this module turns
them into a 3x3 transform plus an honest account of how good it is. The
plan's safety argument (B3) is implemented here: fewer degrees of freedom
means fewer ways to be confidently wrong, so similarity is the default model
and affine — which can shear a sheet into an "alignment" that is really a
distortion — is only fitted on request. Homography is refused outright: it is
opt-in elsewhere and never fitted by this module.

Three layers, in increasing order of caution:

* ``fit_transform`` — weighted least squares, or RANSAC over the weighted
  least-squares model. RANSAC is the default: a handful of wrong
  correspondences (a label the designer moved and reused, a dimension that
  changed) must not be allowed to drag the fit. The final answer is always a
  weighted least-squares refit on the consensus inliers.
* ``cross_validate`` — the anti-overfitting check (B6 metric 7). Fit on 80%
  of the correspondences, measure RMS on the held-out 20%, repeat. A fit
  that only works where it was fitted fails here.
* ``validate_transform`` — the transform sanity gate (B6 metric 5): scale in
  a believable range, positive determinant (a negative one mirrors the
  sheet), rotation near an axis for CAD sheets, shear near zero, and the
  fitted scale within tolerance of the metadata prior when one is known.

Decomposition is documented below: the linear part is factored as a rotation
followed by an upper-triangular scale/shear. That factorisation is exact for
similarity matrices (shear is exactly zero) and is the best-effort reading
for general affine matrices, which is all the sanity gate needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from engine.align.types import (
    AlignConfig,
    AlignMethod,
    Correspondence,
    TransformModel,
    TransformResult,
    identity_matrix,
)

#: Smallest correspondence sets that determine each model.
_MIN_POINTS: dict[TransformModel, int] = {
    TransformModel.SIMILARITY: 2,
    TransformModel.AFFINE: 3,
}

#: Shear above this absolute value looks like a real distortion, not noise.
MAX_CAD_SHEAR = 1e-3

#: Similarity fits use 1 + 1 columns of variance to decide if a sample is
#: degenerate: two identical old points cannot determine a similarity.
_DEGENERATE_VARIANCE = 1e-12


# ── Decomposition ────────────────────────────────────────────────────────


@dataclass(slots=True)
class Decomposed:
    """The parameters hidden inside a 3x3 affine matrix."""

    tx: float
    ty: float
    rotation_deg: float
    scale_x: float
    scale_y: float
    shear: float
    determinant: float


def decompose(matrix3x3: NDArray[np.float64]) -> Decomposed:
    """Read translation, rotation, scale and shear out of a 3x3 matrix.

    The linear part ``L`` is factored as ``L = R(theta) @ K`` where ``R`` is
    a rotation by ``theta = atan2(L10, L00)`` — the direction the old x axis
    is mapped to — and ``K = [[scale_x, shear], [0, scale_y]]`` is the scale
    and shear in the rotated frame:

    * ``scale_x`` is the length of the first column of ``L``;
    * ``scale_y`` follows from the determinant (``det = scale_x * scale_y``);
    * ``shear`` is the second column's component along the rotated x axis.

    For a similarity matrix (uniform scale, no shear) this is exact:
    ``scale_x == scale_y`` and ``shear == 0`` up to floating point. For a
    general affine matrix it is one valid factorisation among several — the
    rotation and scales swap roles under transposition — so treat the affine
    reading as best-effort. The quality gate only consumes similarity fits
    and the determinant, which is unambiguous.
    """
    matrix = np.asarray(matrix3x3, dtype=float)
    tx = float(matrix[0, 2])
    ty = float(matrix[1, 2])
    m00, m01, m10, m11 = (float(value) for value in matrix[:2, :2].flat)

    determinant = m00 * m11 - m01 * m10
    scale_x = math.hypot(m00, m10)
    if scale_x > _DEGENERATE_VARIANCE:
        # R(theta) with cos = m00/scale_x, sin = m10/scale_x rotates the old
        # x axis onto the first column, so K = R^T @ L is upper triangular.
        rotation_deg = math.degrees(math.atan2(m10, m00))
        shear = (m00 * m01 + m10 * m11) / scale_x
        scale_y = determinant / scale_x
    else:
        # The first column vanished (a pathological matrix): report what is
        # left of the second column rather than dividing by zero.
        rotation_deg = 0.0
        shear = 0.0
        scale_y = math.copysign(math.hypot(m01, m11), determinant)

    return Decomposed(
        tx=tx,
        ty=ty,
        rotation_deg=rotation_deg,
        scale_x=scale_x,
        scale_y=scale_y,
        shear=shear,
        determinant=determinant,
    )


def similarity_matrix(
    tx: float, ty: float, rotation_deg: float, scale: float
) -> NDArray[np.float64]:
    """Build the 3x3 matrix of a similarity: translate, rotate, uniform scale.

    The linear part is ``scale * [[cos, -sin], [sin, cos]]``, the same
    convention :func:`decompose` inverts, so building and decomposing round
    trips exactly.
    """
    theta = math.radians(rotation_deg)
    cosine = math.cos(theta)
    sine = math.sin(theta)
    return np.asarray(
        [
            [scale * cosine, -scale * sine, tx],
            [scale * sine, scale * cosine, ty],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


def affine_matrix_from_2x2(
    m2x2: NDArray[np.float64] | list[list[float]] | tuple[tuple[float, float], ...],
    tx: float,
    ty: float,
) -> NDArray[np.float64]:
    """Build a 3x3 affine matrix from its linear part plus translation."""
    linear = np.asarray(m2x2, dtype=float)
    if linear.shape != (2, 2):
        raise ValueError(f"the linear part must be 2x2, got shape {linear.shape}")
    matrix = identity_matrix()
    matrix[:2, :2] = linear
    matrix[0, 2] = tx
    matrix[1, 2] = ty
    return matrix


# ── Weighted least squares ──────────────────────────────────────────────


def _fit_similarity_weighted(
    old: NDArray[np.float64],
    new: NDArray[np.float64],
    weights: NDArray[np.float64],
) -> NDArray[np.float64] | None:
    """Weighted Umeyama closed form for ``new = s R old + t``.

    Exact least squares in closed form: centroid-weighted covariance, SVD for
    the rotation (reflection rejected — a similarity fit never mirrors), the
    scale from the projection, then translation from the centroids. Returns
    None when the fit is under-determined (all old points coincide).
    """
    total_weight = float(np.sum(weights))
    if total_weight <= 0.0:
        return None
    mu_old = np.average(old, axis=0, weights=weights)
    mu_new = np.average(new, axis=0, weights=weights)
    centered_old = old - mu_old
    centered_new = new - mu_new
    variance_old = float(np.sum(weights * np.sum(centered_old**2, axis=1)))
    if variance_old <= _DEGENERATE_VARIANCE:
        return None

    # Covariance of old against new, weighted: H = sum_i w_i yc_i xc_i^T.
    covariance = (centered_new * weights[:, None]).T @ centered_old
    u, _, vt = np.linalg.svd(covariance)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0.0:
        # The best orthogonal map is a reflection; flip one axis so the
        # fitted similarity stays a true rotation (Umeyama's S).
        u = u.copy()
        u[:, -1] *= -1.0
        rotation = u @ vt

    scale = float(np.trace(rotation.T @ covariance)) / variance_old
    matrix = identity_matrix()
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = mu_new - scale * (rotation @ mu_old)
    return matrix


def _fit_affine_weighted(
    old: NDArray[np.float64],
    new: NDArray[np.float64],
    weights: NDArray[np.float64],
) -> NDArray[np.float64] | None:
    """Weighted least squares for a full affine model (6 degrees of freedom).

    Each row is scaled by the square root of its weight and solved with
    ``lstsq`` for both output coordinates at once. Returns None when the old
    points are degenerate (rank < 3 — collinear or coincident).
    """
    if len(old) < 3 or float(np.sum(weights)) <= 0.0:
        return None
    sqrt_weights = np.sqrt(weights)
    design = np.column_stack([old, np.ones(len(old), dtype=float)])
    coefficients, _, rank, _ = np.linalg.lstsq(
        design * sqrt_weights[:, None], new * sqrt_weights[:, None], rcond=None
    )
    if rank < 3:
        return None
    matrix = identity_matrix()
    matrix[0, 0] = coefficients[0, 0]
    matrix[1, 0] = coefficients[0, 1]
    matrix[0, 1] = coefficients[1, 0]
    matrix[1, 1] = coefficients[1, 1]
    matrix[0, 2] = coefficients[2, 0]
    matrix[1, 2] = coefficients[2, 1]
    return matrix


def _forward_errors(
    matrix: NDArray[np.float64],
    old: NDArray[np.float64],
    new: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Per-correspondence old->new residual in pixels after applying matrix."""
    projected = old @ matrix[:2, :2].T + matrix[:2, 2]
    return np.linalg.norm(projected - new, axis=1)


# ── Fitting ──────────────────────────────────────────────────────────────


def _populate(
    result: TransformResult,
    matrix: NDArray[np.float64],
    errors: NDArray[np.float64],
    inlier_mask: NDArray[np.bool_],
) -> TransformResult:
    """Fill the fit statistics of *result* from the final matrix and errors."""
    result.matrix = matrix
    total = len(errors)
    result.inlier_count = int(np.sum(inlier_mask))
    result.inlier_ratio = float(result.inlier_count / total) if total else 0.0
    inlier_errors = errors[inlier_mask]
    result.rms_residual_px = (
        float(np.sqrt(np.mean(inlier_errors**2))) if len(inlier_errors) else float("inf")
    )
    result.residuals_px = [float(error) for error in errors]

    decomposed = decompose(matrix)
    result.tx_px = decomposed.tx
    result.ty_px = decomposed.ty
    result.rotation_deg = decomposed.rotation_deg
    result.shear = decomposed.shear
    result.determinant = decomposed.determinant
    # One scale number for the sanity gate: the geometric mean of the two
    # axis scales, which equals the uniform scale for a similarity.
    result.scale = math.sqrt(abs(decomposed.determinant))
    return result


def fit_transform(
    correspondences: list[Correspondence],
    model: TransformModel = TransformModel.SIMILARITY,
    *,
    method: str = "ransac",
    config: AlignConfig | None = None,
    method_name: AlignMethod | None = None,
) -> TransformResult:
    """Fit *model* to point correspondences, old sheet -> new sheet.

    ``method``:
    * ``"ls"`` — weighted least squares over every correspondence
      (Umeyama closed form for similarity, ``lstsq`` for affine).
    * ``"ransac"`` (default) — sample minimal subsets, score each candidate
      by forward old->new error under ``config.ransac_inlier_threshold_px``,
      keep the largest consensus, then refit weighted least squares on the
      inliers. If the consensus is below ``config.min_inlier_ratio`` the best
      fit is still returned, flagged in ``note`` — refusing loudly is the
      orchestrator's job, and it needs the numbers to explain itself.

    ``method_name`` records where the correspondences came from in
    ``result.method``. Callers whose method is a CAD-content strategy (text
    anchors, grid bubbles) must pass it, or the rotation-sanity check in
    :func:`validate_transform` cannot apply; the type-level default is
    ``manual``, which the gate exempts.

    A fit that cannot be determined (too few correspondences, coincident or
    collinear points) returns a result whose matrix is the identity and whose
    RMS is infinite — never a confident guess. ``model`` may not be
    homography: it is never fitted automatically.
    """
    if model == TransformModel.HOMOGRAPHY:
        raise ValueError("homography requires explicit user opt-in and is never fitted here")
    config = config or AlignConfig()
    result = TransformResult(model=model, correspondence_count=len(correspondences))
    if method_name is not None:
        result.method = method_name

    needed = _MIN_POINTS[model]
    if len(correspondences) < needed:
        result.note = (
            f"a {model.value} fit needs at least {needed} correspondences; "
            f"only {len(correspondences)} were found"
        )
        return result

    old = np.asarray([(item.old_x, item.old_y) for item in correspondences], dtype=float)
    new = np.asarray([(item.new_x, item.new_y) for item in correspondences], dtype=float)
    weights = np.asarray([item.weight for item in correspondences], dtype=float)
    fit = _fit_similarity_weighted if model == TransformModel.SIMILARITY else _fit_affine_weighted

    chosen_method = str(method).lower()
    if chosen_method == "ls":
        matrix = fit(old, new, weights)
        if matrix is None:
            result.note = (
                "the correspondences are degenerate for a least-squares fit "
                "(coincident or collinear points)"
            )
            return result
        errors = _forward_errors(matrix, old, new)
        return _populate(result, matrix, errors, np.ones(len(errors), dtype=bool))
    if chosen_method != "ransac":
        raise ValueError(f"unknown fitting method {method!r}; expected 'ls' or 'ransac'")

    rng = np.random.default_rng()
    threshold = config.ransac_inlier_threshold_px
    best_mask: NDArray[np.bool_] | None = None
    best_count = 0
    best_sse = math.inf
    for _ in range(config.ransac_iterations):
        sample = rng.choice(len(old), size=needed, replace=False)
        candidate = fit(old[sample], new[sample], weights[sample])
        if candidate is None:
            continue  # degenerate minimal sample: skip it
        errors = _forward_errors(candidate, old, new)
        mask = errors < threshold
        count = int(np.sum(mask))
        sse = float(np.sum(errors[mask] ** 2))
        if count > best_count or (count == best_count and sse < best_sse):
            best_count = count
            best_sse = sse
            best_mask = mask

    if best_mask is None or not np.any(best_mask):
        result.note = "RANSAC found no valid transform — the correspondences are degenerate"
        return result

    matrix = fit(old[best_mask], new[best_mask], weights[best_mask])
    if matrix is None:
        result.note = "the RANSAC inliers are degenerate for a refit"
        return result
    errors = _forward_errors(matrix, old, new)
    final_mask = errors < threshold
    result = _populate(result, matrix, errors, final_mask)
    if result.inlier_ratio < config.min_inlier_ratio:
        result.note = (
            f"inlier ratio {result.inlier_ratio:.2f} is below the minimum "
            f"{config.min_inlier_ratio:.2f} — treat this fit as unreliable"
        )
    return result


# ── Cross-validation ─────────────────────────────────────────────────────


def cross_validate(
    correspondences: list[Correspondence],
    model: TransformModel = TransformModel.SIMILARITY,
    *,
    holdout: float = 0.2,
    repeats: int = 5,
    config: AlignConfig | None = None,
) -> tuple[float, float]:
    """(mean, std) RMS in px of held-out correspondences.

    Each fold splits the set deterministically (``np.random.default_rng(42 +
    fold)``), refits on the training 80% with plain weighted least squares
    and measures the RMS of the held-out 20%. A clean correspondence set
    scores near zero; a set that includes a cluster of wrong matches scores
    badly, because every fold has to fit through or around the lies. Returns
    (NaN, NaN) when the set is too small to hold anything out.
    """
    if model == TransformModel.HOMOGRAPHY:
        raise ValueError("homography requires explicit user opt-in and is never fitted here")
    config = config or AlignConfig()
    total = len(correspondences)
    if total < _MIN_POINTS[model] + 1:
        return float("nan"), float("nan")
    holdout_count = max(1, round(total * holdout))
    if total - holdout_count < _MIN_POINTS[model]:
        return float("nan"), float("nan")

    fold_rms: list[float] = []
    for fold in range(repeats):
        rng = np.random.default_rng(42 + fold)
        order = rng.permutation(total)
        train_indices = order[holdout_count:]
        test_indices = order[:holdout_count]
        train = [correspondences[int(index)] for index in train_indices]
        fit = fit_transform(train, model, method="ls", config=config)
        if not np.isfinite(fit.rms_residual_px):
            continue  # this fold's training set was degenerate; skip it
        test_old = np.asarray(
            [
                (correspondences[int(index)].old_x, correspondences[int(index)].old_y)
                for index in test_indices
            ],
            dtype=float,
        )
        test_new = np.asarray(
            [
                (correspondences[int(index)].new_x, correspondences[int(index)].new_y)
                for index in test_indices
            ],
            dtype=float,
        )
        errors = _forward_errors(fit.matrix, test_old, test_new)
        fold_rms.append(float(np.sqrt(np.mean(errors**2))))

    if not fold_rms:
        return float("nan"), float("nan")
    return float(np.mean(fold_rms)), float(np.std(fold_rms))


# ── Validation ───────────────────────────────────────────────────────────


def validate_transform(
    result: TransformResult,
    *,
    expected_scale: float | None = None,
    is_scanned: bool = False,
    allow_mirror: bool = False,
    rotation_sanity_deg: float = 2.0,
    scale_tolerance: float = 0.15,
) -> list[str]:
    """Plain-English reasons this transform is not believable. Empty = valid.

    The transform sanity half of the quality gate (B6 metric 5):

    * scale must sit in the 0.2x to 5x range (config defaults);
    * a negative determinant means the fit mirrors the sheet — rejected
      unless ``allow_mirror``;
    * for CAD sheets (``is_scanned`` False) and content methods, rotation
      must be within ``rotation_sanity_deg`` of 0/90/180/270 and shear must
      be near zero. Scanned sheets and sheet-border/manual methods are
      exempt — scans arrive skewed and rotated, and manual points are the
      user's own truth;
    * when ``expected_scale`` is known (metadata prior, B4 method 6) the
      fitted scale must be within ``scale_tolerance`` of it.
    """
    bounds = AlignConfig()
    problems: list[str] = []

    if not bounds.min_scale <= result.scale <= bounds.max_scale:
        problems.append(
            f"the fitted scale is {result.scale:.2f}x, outside the believable "
            f"{bounds.min_scale:g}x to {bounds.max_scale:g}x range"
        )
    if result.determinant <= 0.0 and not allow_mirror:
        problems.append("would mirror the sheet (negative determinant); mirroring is not enabled")

    exempt = {AlignMethod.SHEET_BORDER, AlignMethod.MANUAL}
    if not is_scanned and result.method not in exempt:
        axis_distance = abs(((result.rotation_deg + 45.0) % 90.0) - 45.0)
        if axis_distance > rotation_sanity_deg:
            problems.append(
                f"rotation is {result.rotation_deg:.1f} degrees, not within "
                f"{rotation_sanity_deg:g} degrees of 0, 90, 180 or 270 for a CAD sheet"
            )
        if abs(result.shear) > MAX_CAD_SHEAR:
            problems.append(
                f"the fit includes shear ({result.shear:.4g}) — a CAD sheet must not be sheared"
            )

    if expected_scale is not None and expected_scale > 0.0:
        mismatch = abs(result.scale - expected_scale) / expected_scale
        if mismatch > scale_tolerance:
            problems.append(
                f"fitted scale {result.scale:.3f} does not match the drawing "
                f"scales (expected {expected_scale:.3f})"
            )
    return problems
