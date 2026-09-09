"""Grid bubble detection and matching for alignment (Phase 4, task 4.5).

Implements method 2 of the alignment cascade in
``Plans/PHASE-4-Render-Align-Plan.md`` (section B4): construction drawings
carry rows and columns of small labelled circles — grid bubbles — at the ends
of the grid lines. They are few, well separated across the sheet, and stable
between revisions, so when text extraction is thin or absent they are the
best anchors available.

Pipeline in :func:`detect_bubbles`:

* :func:`cv2.HoughCircles` over a radius range converted from the expected
  bubble size on paper (``AlignConfig.bubble_min_mm`` to ``bubble_max_mm``
  via *px_per_mm_value*), Gaussian-blurred (7x7) first, dp=1.2, param1=100,
  param2 starting at 30 and escalating only when the raw candidate list is
  noise-dominated.
* Three geometric filters, one function each:
  :func:`_reject_filled` (the interior of a bubble is paper, not ink),
  the edge-preference step (interior circles are kept but lose half their
  confidence — the plan's lower-confidence group) and
  :func:`_keep_collinear_runs` (only circles that belong to a horizontal or
  vertical run of at least three survive the false-positive filter).

Labels are attached through the caller-supplied ``label_for`` callback.
This project has **no OCR provider**, so scanned-sheet labels stay ``""``
until one exists: the docstring of :func:`match_bubbles` explains the
consequences honestly. For CAD-exported sheets the orchestrator can supply
labels from the PDF text layer whose centre falls inside each circle.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.align.types import AlignConfig, Correspondence
from engine.align.units import px_per_mm

#: Gaussian blur kernel applied before Hough (plan: 7x7).
_BLUR_KERNEL = 7
#: HoughCircles dp — accumulator resolution relative to the image.
_HOUGH_DP = 1.2
#: HoughCircles Canny high threshold (param1).
_HOUGH_CANNY_HIGH = 100.0
#: Accumulator thresholds (param2) tried in order. Start at 30 per the plan
#: and escalate only when the raw candidate list looks noise-dominated.
_ACCUMULATOR_STARTS = (30.0, 50.0, 75.0)
#: Above this many raw candidates a pass is treated as noise-dominated and
#: the detector re-runs with a stricter accumulator threshold.
_MAX_RAW_CANDIDATES = 150
#: Fraction of the radius used to sample the interior of a candidate circle.
_INTERIOR_SAMPLE_FRACTION = 0.6
#: Interior mean grey (0..255) below which a circle is treated as a filled
#: shape rather than a bubble (plan: reject interiors that are mostly ink).
_FILLED_MEAN_MAX = 128.0
#: Confidence factor for interior-only circles (edge preference, plan B4-2).
_INTERIOR_CONFIDENCE = 0.5
#: Confidence factor for a member of a run that sits on a clean row/column.
_RUN_CONFIDENCE = 0.85
#: A run is "clean" when every member lies within this many pixels of the
#: run mean, or within this fraction of the run's mean radius, whichever is
#: larger.
_CLEAN_RUN_MIN_PX = 2.0
_CLEAN_RUN_FRACTION = 0.06
#: Normalising radius for correspondence weights: a typical 10 mm grid
#: bubble at the 200 DPI comparison standard, so an average pair weighs ~1.
_REFERENCE_RADIUS_PX = 10.0 * px_per_mm(200)


@dataclass(slots=True)
class GridBubble:
    """One detected grid bubble on one sheet, in image pixel coordinates.

    ``centre`` and ``radius_px`` are OpenCV's estimates; ``label`` is filled
    by the caller's ``label_for`` callback when one is supplied (see
    :func:`detect_bubbles` for the OCR caveat).
    """

    centre: tuple[float, float]
    radius_px: float
    label: str = ""
    #: Detection confidence in 0..1. Starts at 1.0 per candidate; interior
    #: circles are halved (edge preference) and grid-run members on a clean
    #: row/column carry 0.85 (structural match, label not yet verified).
    confidence: float = 0.0
    #: Dark-ink fraction (0..1) of the centred sample disc (0.6 x radius).
    #: Real bubbles stay near paper white (~0); filled shapes approach 1.
    interior_ink_fraction: float = 0.0

    @property
    def x(self) -> float:
        """Horizontal centre in image pixels."""
        return self.centre[0]

    @property
    def y(self) -> float:
        """Vertical centre in image pixels."""
        return self.centre[1]


def detect_bubbles(
    image: NDArray[np.uint8],
    px_per_mm_value: float,
    config: AlignConfig | None = None,
    *,
    label_for: Callable[[tuple[float, float]], str | None] | None = None,
) -> list[GridBubble]:
    """Detect grid bubbles on one rasterised sheet.

    *image* is 8-bit grayscale or a 3-channel colour array (treated as BGR,
    the OpenCV convention the renderers use; convert RGB arrays with
    ``cv2.cvtColor(..., cv2.COLOR_RGB2BGR)`` first). Paper must be light and
    ink dark, as the renderer produces.

    The Hough radius range is derived from the expected bubble size on paper:
    ``config.bubble_min_mm``/``bubble_max_mm`` (default 8-12 mm) converted
    through *px_per_mm_value* — never hard-coded pixels. Pass 0 or a
    non-positive *px_per_mm_value* to raise :class:`ValueError`.

    **Filters** (each documented in its own function):

    1. :func:`_reject_filled` — filled shapes are hatching or stamps, not
       bubbles.
    2. Edge preference — circles hugging the sheet border (within
       ``config.bubble_edge_fraction`` of an edge) are the stereotype;
       interior circles are *kept* but their confidence is halved (plan:
       interior bubbles are a lower-confidence group).
    3. :func:`_keep_collinear_runs` — real grid bubbles line up in rows and
       columns: centres are clustered by x and by y within 2x the circle
       radius, and only circles belonging to a horizontal or vertical run of
       three or more are returned.

    **Labels and OCR:** there is no OCR provider in this project. When
    *label_for* is given it is called with each kept circle's centre and its
    return value is stored verbatim as ``label`` (the caller decides
    normalisation, e.g. NFKC/upper-case like the text anchors). Without a
    callback — or on a scanned sheet whose text layer is absent — labels stay
    ``""``; reading the label out of a scanned crop is out of scope until an
    OCR provider exists.

    Returns the surviving circles in deterministic order — top to bottom,
    then left to right. Raises :class:`ValueError` when the image or the
    configuration cannot represent the configured bubble sizes.
    """
    cfg = config if config is not None else AlignConfig()
    if px_per_mm_value <= 0:
        raise ValueError(f"px_per_mm_value must be positive, got {px_per_mm_value!r}.")
    if not 0.0 < cfg.bubble_min_mm <= cfg.bubble_max_mm:
        raise ValueError(
            "AlignConfig bubble radius range must satisfy "
            f"0 < bubble_min_mm <= bubble_max_mm, got min={cfg.bubble_min_mm!r}, "
            f"max={cfg.bubble_max_mm!r}."
        )
    if not 0.0 <= cfg.bubble_edge_fraction < 1.0:
        raise ValueError(
            f"AlignConfig bubble_edge_fraction must be in [0, 1), got {cfg.bubble_edge_fraction!r}."
        )
    gray = _as_grayscale(image)
    height, width = gray.shape
    min_radius_px = cfg.bubble_min_mm * px_per_mm_value
    max_radius_px = cfg.bubble_max_mm * px_per_mm_value
    if max_radius_px < 4.0:
        raise ValueError(
            "Grid bubbles cannot be represented at this resolution: "
            f"{cfg.bubble_max_mm} mm at {px_per_mm_value:.3f} px/mm is only "
            f"{max_radius_px:.1f} px. Detect on the full-resolution render."
        )

    blurred = cv2.GaussianBlur(gray, (_BLUR_KERNEL, _BLUR_KERNEL), 0.0)
    candidates = _hough_circles(blurred, min_radius_px, max_radius_px)
    raw_count = len(candidates)
    if not candidates:
        return []
    candidates = _reject_filled(candidates, gray)
    _mark_edge_confidence(candidates, width, height, cfg.bubble_edge_fraction)
    kept = _keep_collinear_runs(candidates)
    for bubble in kept:
        if label_for is not None:
            label = label_for((bubble.x, bubble.y))
            if label is not None:
                bubble.label = label
    kept.sort(key=lambda bubble: (bubble.centre[1], bubble.centre[0]))
    logger.debug(
        "grid bubble detection: {} raw candidates, {} after filled/edge/run filters",
        raw_count,
        len(kept),
    )
    return kept


def _as_grayscale(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Normalise the accepted input forms to a single uint8 channel.

    A 2D array is returned as-is; a 3-channel array is converted BGR->gray;
    anything else (wrong dtype, four channels, non-array) raises.
    """
    if not isinstance(image, np.ndarray):
        raise TypeError(f"image must be a numpy array, got {type(image).__name__}.")
    if image.dtype != np.uint8:
        raise ValueError(f"image must be uint8, got dtype {image.dtype}.")
    if image.ndim == 2:
        return image
    if image.ndim == 3:
        if image.shape[2] != 3:
            raise ValueError(f"colour image must have 3 channels, got {image.shape[2]}.")
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raise ValueError(f"image must be 2D grayscale or 3-channel colour, got {image.ndim}D.")


def _hough_circles(
    blurred: NDArray[np.uint8], min_radius_px: float, max_radius_px: float
) -> list[GridBubble]:
    """Run :func:`cv2.HoughCircles`, escalating the accumulator threshold.

    dp=1.2, param1=100, param2 starting at 30 (the plan's start value). When
    a pass returns more than :data:`_MAX_RAW_CANDIDATES` circles the result
    is noise-dominated and the detector retries with a stricter threshold.
    ``minDist`` is 2 x the minimum radius, which suppresses duplicate votes
    for the same circle while never separating two real adjacent bubbles.
    """
    min_dist = max(1.0, 2.0 * min_radius_px)
    found: NDArray[np.float32] | None = None
    for param2 in _ACCUMULATOR_STARTS:
        found = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=_HOUGH_DP,
            minDist=min_dist,
            param1=_HOUGH_CANNY_HIGH,
            param2=param2,
            minRadius=round(min_radius_px),
            maxRadius=round(max_radius_px),
        )
        if found is None or int(found.shape[1]) <= _MAX_RAW_CANDIDATES:
            break
    if found is None:
        return []
    bubbles: list[GridBubble] = []
    for cx, cy, radius in found[0]:
        bubbles.append(
            GridBubble(
                centre=(float(cx), float(cy)),
                radius_px=float(radius),
                confidence=1.0,
            )
        )
    return bubbles


def _reject_filled(bubbles: list[GridBubble], gray: NDArray[np.uint8]) -> list[GridBubble]:
    """Filter 1 — drop circles whose interior is mostly ink.

    A grid bubble is an outline: apart from its small label, the interior of
    the circle is paper. Filled discs (hatching, stamps, filled symbols) are
    not bubbles. Each candidate is sampled over a centred disc of
    ``_INTERIOR_SAMPLE_FRACTION x radius``; when the sample mean is darker
    than ``_FILLED_MEAN_MAX`` (128) the circle is rejected. The measured
    dark-ink fraction is recorded on every returned bubble as
    ``interior_ink_fraction``.
    """
    kept: list[GridBubble] = []
    for bubble in bubbles:
        bubble.interior_ink_fraction = _interior_ink_fraction(gray, bubble)
        mean_grey = (1.0 - bubble.interior_ink_fraction) * 255.0
        if mean_grey < _FILLED_MEAN_MAX:
            continue
        kept.append(bubble)
    return kept


def _interior_ink_fraction(gray: NDArray[np.uint8], bubble: GridBubble) -> float:
    """Dark-ink fraction (0..1) of the centred sample disc of one circle.

    Samples a disc of ``_INTERIOR_SAMPLE_FRACTION x radius`` around the
    centre on a small ROI so near-edge circles clip gracefully. A region
    that cannot be sampled at all (centre off-canvas) reports 1.0 — fully
    inked — so it is rejected rather than trusted.
    """
    sample_radius = max(1, math.ceil(bubble.radius_px * _INTERIOR_SAMPLE_FRACTION))
    margin = sample_radius + 1
    cx, cy = round(bubble.x), round(bubble.y)
    height, width = gray.shape
    x0, x1 = max(0, cx - margin), min(width, cx + margin + 1)
    y0, y1 = max(0, cy - margin), min(height, cy + margin + 1)
    if x1 <= x0 or y1 <= y0:
        return 1.0
    patch = gray[y0:y1, x0:x1]
    mask = np.zeros_like(patch)
    cv2.circle(mask, (cx - x0, cy - y0), sample_radius, 255, -1)
    if int(mask.sum()) == 0:
        return 1.0
    mean_grey = cv2.mean(patch, mask=mask)[0]
    return float(max(0.0, 1.0 - mean_grey / 255.0))


def _mark_edge_confidence(
    bubbles: list[GridBubble],
    width: int,
    height: int,
    edge_fraction: float,
) -> None:
    """Filter 2 — keep interior circles but halve their confidence.

    Grid bubbles cluster along the sheet edges, so a circle whose centre lies
    within ``edge_fraction`` of an image edge (in either axis) is the
    stereotype and keeps its confidence. Circles deeper inside the sheet are
    kept — they still pass the run filter — but their confidence is
    multiplied by :data:`_INTERIOR_CONFIDENCE` (0.5), the plan's
    lower-confidence group. Mutates the bubbles in place.
    """
    for bubble in bubbles:
        gap_x = min(bubble.x, width - bubble.x)
        gap_y = min(bubble.y, height - bubble.y)
        near_edge = gap_x <= edge_fraction * width or gap_y <= edge_fraction * height
        if not near_edge:
            bubble.confidence *= _INTERIOR_CONFIDENCE


def _keep_collinear_runs(bubbles: list[GridBubble]) -> list[GridBubble]:
    """Filter 3 — keep only circles on a horizontal or vertical run of >= 3.

    Grid bubbles line up: the circles of one row share a y and the circles of
    one column share an x. Centres are clustered along each axis within
    ``2 x radius`` (of the circle being added) by :func:`_axis_clusters`, and
    a circle survives only if it belongs to at least one run of three or
    more — a circle in a run of two, or in no run, is a false positive.

    Members of a *clean* run — every centre within
    ``max(_CLEAN_RUN_MIN_PX, _CLEAN_RUN_FRACTION x mean radius)`` of the run
    mean, i.e. a tight row/column rather than a wobble — are the stereotypic
    grid bubble and keep most of their confidence
    (x :data:`_RUN_CONFIDENCE`); the small deduction records that the match
    is structural (grid-aligned), not label-verified.
    """
    members: set[int] = set()
    clean_members: set[int] = set()
    for horizontal in (True, False):
        for run in _axis_clusters(bubbles, horizontal):
            if len(run) < 3:
                continue
            run_ids = {id(bubble) for bubble in run}
            members |= run_ids
            if _is_clean_run(run, horizontal):
                clean_members |= run_ids
    kept = [bubble for bubble in bubbles if id(bubble) in members]
    for bubble in kept:
        if id(bubble) in clean_members:
            bubble.confidence *= _RUN_CONFIDENCE
    return kept


def _axis_clusters(bubbles: list[GridBubble], horizontal: bool) -> list[list[GridBubble]]:
    """Group circles that share a row (y) or a column (x).

    Circles within ``2 x radius`` of the running cluster mean along the axis
    are treated as sharing that row or column. One greedy pass over the
    axis-sorted centres keeps the result deterministic; the running mean
    absorbs sub-pixel detection jitter.
    """
    ordered = sorted(bubbles, key=lambda b: _axis_coordinate(b, horizontal))
    clusters: list[list[GridBubble]] = []
    for bubble in ordered:
        coordinate = _axis_coordinate(bubble, horizontal)
        if clusters:
            cluster = clusters[-1]
            mean = sum(_axis_coordinate(member, horizontal) for member in cluster)
            mean /= len(cluster)
            if abs(coordinate - mean) <= 2.0 * bubble.radius_px:
                cluster.append(bubble)
                continue
        clusters.append([bubble])
    return clusters


def _is_clean_run(run: list[GridBubble], horizontal: bool) -> bool:
    """True when every member of *run* lies tightly on its row/column line."""
    mean = sum(_axis_coordinate(bubble, horizontal) for bubble in run) / len(run)
    deviation = max(abs(_axis_coordinate(bubble, horizontal) - mean) for bubble in run)
    mean_radius = sum(bubble.radius_px for bubble in run) / len(run)
    tolerance = max(_CLEAN_RUN_MIN_PX, _CLEAN_RUN_FRACTION * mean_radius)
    return deviation <= tolerance


def _axis_coordinate(bubble: GridBubble, horizontal: bool) -> float:
    """The coordinate that defines a row (y) or a column (x)."""
    return bubble.y if horizontal else bubble.x


def match_bubbles(
    old_bubbles: list[GridBubble],
    new_bubbles: list[GridBubble],
) -> list[Correspondence]:
    """Match grid bubbles between an old and a new issue by label.

    A label must appear **exactly once on each sheet** to produce a
    correspondence — the text-anchor rule from plan task 4.4/4.5: a label
    that repeats on either side is ambiguous and is discarded entirely.

    Empty labels never match. Without a ``label_for`` callback (or an OCR
    provider, which this project does not have) every detected bubble has
    ``label == ""``; matching them positionally would pair unrelated circles,
    so :func:`match_bubbles` returns nothing in that case. The orchestrator
    is expected to attach PDF text-layer labels before matching CAD sheets,
    and to normalise them the way text anchors are normalised, because
    comparisons here are exact string comparisons.

    Each correspondence weight is ``sqrt(radius_old x radius_new) /
    _REFERENCE_RADIUS_PX`` — normalised so an average pair weighs about 1.0
    and bigger bubbles weigh more, mirroring the text-anchor rule that
    larger marks are more reliable. The result is deterministic: sorted by
    label.
    """
    old_once = _singly_occurring(old_bubbles)
    new_once = _singly_occurring(new_bubbles)
    matches: list[Correspondence] = []
    for label, old_bubble in old_once.items():
        new_bubble = new_once.get(label)
        if new_bubble is None:
            continue
        radius_product = max(old_bubble.radius_px, 0.0) * max(new_bubble.radius_px, 0.0)
        weight = math.sqrt(radius_product) / _REFERENCE_RADIUS_PX
        matches.append(
            Correspondence(
                old_x=old_bubble.x,
                old_y=old_bubble.y,
                new_x=new_bubble.x,
                new_y=new_bubble.y,
                weight=weight,
                label=label,
            )
        )
    matches.sort(key=lambda correspondence: correspondence.label)
    return matches


def _singly_occurring(bubbles: list[GridBubble]) -> dict[str, GridBubble]:
    """The bubbles whose non-empty label occurs exactly once in *bubbles*."""
    counts = Counter(bubble.label for bubble in bubbles if bubble.label)
    return {
        bubble.label: bubble for bubble in bubbles if bubble.label and counts[bubble.label] == 1
    }
