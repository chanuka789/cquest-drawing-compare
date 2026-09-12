"""Task 5.11 — the raster stream: the safety net, and a tolerant one.

The raster stream always runs, because it is the only one that works on a
scan. But a plain pixel XOR of two line drawings is useless: every line has
slightly different anti-aliasing, and a one-pixel line weight change lights
up the entire sheet.

The fix is to stop asking "is there ink exactly here?" and start asking
**"is there ink nearby?"**. A distance transform of the new sheet's ink gives
every pixel its distance to the nearest new ink; old ink closer than the
tolerance is matched. Run it in both directions and what is left over is the
actual difference. One operation absorbs line weight change, sub-pixel
jitter, anti-aliasing and small alignment residual.

Performance follows the plan: find candidate regions on a quarter-size image,
then re-measure only those regions at full resolution. Most of a sheet is
unchanged and there is no reason to pay full price for it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.compare.tolerance import ResolvedTolerance, area_px_to_site_m2
from engine.compare.types import Bbox, ChangeKind, ChangeRecord, Stream
from engine.masking.mask_engine import MaskSet, apply_mask_to_image

#: Candidate detection runs at this fraction of full size.
COARSE_SCALE = 0.25
#: Speckle smaller than this many pixels is anti-aliasing, not a change.
MIN_COMPONENT_PX = 6
#: Adaptive thresholding kicks in above this local-variation level, which is
#: what an unevenly lit scan looks like and a plotted PDF never does.
SCAN_VARIATION_THRESHOLD = 8.0


@dataclass(slots=True)
class RasterDiffConfig:
    """Knobs for the raster stream."""

    coarse_scale: float = COARSE_SCALE
    min_component_px: int = MIN_COMPONENT_PX
    #: "auto" picks adaptive for scans and Otsu for plots.
    binarise: str = "auto"
    adaptive_block: int = 31
    adaptive_offset: int = 10
    #: Opening kernel, which removes isolated single pixels.
    opening_px: int = 2
    #: Skip the coarse pass below this pixel count; it costs more than it saves.
    coarse_minimum_px: int = 1_000_000


@dataclass(slots=True)
class RasterRegion:
    """One connected region of genuine difference."""

    bbox: Bbox
    pixel_area: float
    #: True when the new sheet has ink the old one did not.
    added: bool
    removed: bool

    @property
    def kind(self) -> ChangeKind:
        if self.added and not self.removed:
            return ChangeKind.ADDED
        if self.removed and not self.added:
            return ChangeKind.REMOVED
        return ChangeKind.MODIFIED


@dataclass(slots=True)
class RasterDiffResult:
    """What the raster stream found, plus the masks other steps need."""

    changes: list[ChangeRecord] = field(default_factory=list)
    regions: list[RasterRegion] = field(default_factory=list)
    #: The unmatched-ink mask at full resolution; the residual detector reads it.
    change_mask: NDArray[np.uint8] | None = None
    old_binary: NDArray[np.uint8] | None = None
    new_binary: NDArray[np.uint8] | None = None
    old_stroke_width_px: float = 0.0
    new_stroke_width_px: float = 0.0
    duration_s: float = 0.0
    skip_reason: str = ""

    @property
    def ran(self) -> bool:
        return not self.skip_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "regions": len(self.regions),
            "old_stroke_width_px": round(self.old_stroke_width_px, 2),
            "new_stroke_width_px": round(self.new_stroke_width_px, 2),
            "duration_s": round(self.duration_s, 3),
            "skip_reason": self.skip_reason,
        }


# ── Preparation ─────────────────────────────────────────────────────────


def warp_to_new(
    old_gray: NDArray[np.uint8],
    transform: NDArray[np.float64] | None,
    size: tuple[int, int],
) -> NDArray[np.uint8]:
    """Put the old sheet into the new sheet's pixel space.

    ``INTER_NEAREST`` on purpose: interpolating a line drawing invents grey
    edge pixels that then read as differences everywhere the transform was
    not a whole number of pixels.
    """
    width, height = size
    if transform is None:
        if old_gray.shape[:2] == (height, width):
            return old_gray
        return cv2.resize(old_gray, (width, height), interpolation=cv2.INTER_NEAREST)

    matrix = np.asarray(transform, dtype=float)
    if matrix.shape == (3, 3) and np.allclose(matrix[2], [0.0, 0.0, 1.0]):
        return cv2.warpAffine(
            old_gray,
            matrix[:2],
            (width, height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
    return cv2.warpPerspective(
        old_gray,
        matrix,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def looks_scanned(gray: NDArray[np.uint8]) -> bool:
    """Uneven background lighting is what separates a scan from a plot."""
    if gray.size == 0:
        return False
    small = cv2.resize(gray, (256, 256), interpolation=cv2.INTER_AREA)
    background = cv2.medianBlur(small, 21)
    return float(np.std(background)) > SCAN_VARIATION_THRESHOLD


def binarise(gray: NDArray[np.uint8], config: RasterDiffConfig) -> NDArray[np.uint8]:
    """Ink as 255, paper as 0.

    Adaptive Gaussian thresholding handles a scan lit unevenly across the
    sheet; Otsu is better on a clean plot, where adaptive invents texture in
    empty paper.
    """
    if gray.size == 0:
        return gray.copy()
    method = config.binarise
    if method == "auto":
        method = "adaptive" if looks_scanned(gray) else "otsu"

    if method == "adaptive":
        block = config.adaptive_block | 1  # must be odd
        return cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block,
            config.adaptive_offset,
        )
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]


def estimate_stroke_width(binary: NDArray[np.uint8]) -> float:
    """Typical stroke width in pixels, from the distance transform.

    The distance transform's value on a stroke's centreline is half its
    width, so the median over the skeleton is a robust estimate of the pen.
    Used by the line weight filter to normalise before comparing.
    """
    if binary is None or binary.size == 0 or not binary.any():
        return 0.0
    distance = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    try:
        skeleton = cv2.ximgproc.thinning(binary)  # type: ignore[attr-defined]
        values = distance[skeleton > 0]
    except (AttributeError, cv2.error):
        # No contrib build: ridge pixels are those the distance transform
        # gives a local maximum, which is the skeleton by another name.
        dilated = cv2.dilate(distance, np.ones((3, 3), np.float32))
        values = distance[(distance > 0) & (distance >= dilated - 1e-3)]
    if values.size == 0:
        return 0.0
    return float(np.median(values)) * 2.0


# ── The diff ────────────────────────────────────────────────────────────


def tolerant_difference(
    old_binary: NDArray[np.uint8],
    new_binary: NDArray[np.uint8],
    tolerance_px: float,
) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
    """Ink on each side with no ink within *tolerance_px* on the other.

    Returns (removed, added): old ink the new sheet does not account for, and
    new ink the old sheet does not.
    """
    if old_binary.shape != new_binary.shape:
        raise ValueError("The two images must be the same size before differencing.")

    limit = max(tolerance_px, 0.5)
    # distanceTransform measures distance to the nearest zero pixel, so the
    # ink has to be the zeros: invert.
    distance_to_new = cv2.distanceTransform(255 - new_binary, cv2.DIST_L2, 3)
    distance_to_old = cv2.distanceTransform(255 - old_binary, cv2.DIST_L2, 3)

    removed = ((old_binary > 0) & (distance_to_new > limit)).astype(np.uint8) * 255
    added = ((new_binary > 0) & (distance_to_old > limit)).astype(np.uint8) * 255
    return removed, added


def _components(mask: NDArray[np.uint8], min_area: float) -> list[tuple[int, int, int, int, float]]:
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8
    )
    regions: list[tuple[int, int, int, int, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < min_area:
            continue
        regions.append((int(x), int(y), int(width), int(height), float(area)))
    return regions


def diff_raster(
    old_gray: NDArray[np.uint8],
    new_gray: NDArray[np.uint8],
    transform: NDArray[np.float64] | None,
    tolerance: ResolvedTolerance,
    *,
    mask: MaskSet | None = None,
    config: RasterDiffConfig | None = None,
) -> RasterDiffResult:
    """Compare two rendered sheets with a tolerance, not with an XOR."""
    config = config or RasterDiffConfig()
    started = time.perf_counter()
    result = RasterDiffResult()

    if old_gray is None or new_gray is None or old_gray.size == 0 or new_gray.size == 0:
        result.skip_reason = "One of the two sheets could not be rendered."
        return result

    height, width = new_gray.shape[:2]
    warped = warp_to_new(old_gray, transform, (width, height))

    if mask is not None:
        # Masking before binarising is what lets a pale watermark be removed
        # by value while the drawing under it survives.
        warped = apply_mask_to_image(warped, mask)
        new_gray = apply_mask_to_image(new_gray, mask)

    old_binary = binarise(warped, config)
    new_binary = binarise(new_gray, config)
    result.old_binary = old_binary
    result.new_binary = new_binary
    result.old_stroke_width_px = estimate_stroke_width(old_binary)
    result.new_stroke_width_px = estimate_stroke_width(new_binary)

    tolerance_px = max(tolerance.position.px, 1.0)
    min_area = max(tolerance.min_region.px_area, float(config.min_component_px))

    candidates = _candidate_boxes(old_binary, new_binary, tolerance_px, config)
    removed, added = _difference_masks(old_binary, new_binary, tolerance_px, candidates, config)
    result.change_mask = cv2.bitwise_or(removed, added)

    for x, y, box_width, box_height, area in _components(result.change_mask, min_area):
        patch_removed = removed[y : y + box_height, x : x + box_width]
        patch_added = added[y : y + box_height, x : x + box_width]
        removed_count = int((patch_removed > 0).sum())
        added_count = int((patch_added > 0).sum())
        if removed_count + added_count <= 0:
            continue
        region = RasterRegion(
            bbox=Bbox(float(x), float(y), float(box_width), float(box_height)),
            pixel_area=float(area),
            added=added_count > removed_count * 0.2,
            removed=removed_count > added_count * 0.2,
        )
        result.regions.append(region)
        result.changes.append(_record(region, tolerance))

    result.duration_s = time.perf_counter() - started
    logger.debug(
        "Raster diff | regions={} | stroke {:.2f}->{:.2f} px | {:.2f}s",
        len(result.regions),
        result.old_stroke_width_px,
        result.new_stroke_width_px,
        result.duration_s,
    )
    return result


def _difference_masks(
    old_binary: NDArray[np.uint8],
    new_binary: NDArray[np.uint8],
    tolerance_px: float,
    candidates: list[tuple[int, int, int, int, float]] | None,
    config: RasterDiffConfig,
) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
    """The full-resolution difference, measured only where it can exist.

    When the coarse pass found candidate regions, the distance transform runs
    on those crops alone. On a typical sheet that is a few per cent of the
    page, and it is the whole reason an A1 comparison fits in four seconds.
    """
    kernel = np.ones((config.opening_px, config.opening_px), np.uint8)

    def clean(mask: NDArray[np.uint8]) -> NDArray[np.uint8]:
        if config.opening_px > 1:
            return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        return mask

    if candidates is None:
        removed, added = tolerant_difference(old_binary, new_binary, tolerance_px)
        return clean(removed), clean(added)

    removed_full = np.zeros_like(new_binary)
    added_full = np.zeros_like(new_binary)
    for x, y, width, height, _area in candidates:
        old_patch = old_binary[y : y + height, x : x + width]
        new_patch = new_binary[y : y + height, x : x + width]
        if old_patch.size == 0 or new_patch.size == 0:
            continue
        removed, added = tolerant_difference(old_patch, new_patch, tolerance_px)
        removed_full[y : y + height, x : x + width] |= clean(removed)
        added_full[y : y + height, x : x + width] |= clean(added)
    return removed_full, added_full


def _candidate_boxes(
    old_binary: NDArray[np.uint8],
    new_binary: NDArray[np.uint8],
    tolerance_px: float,
    config: RasterDiffConfig,
) -> list[tuple[int, int, int, int, float]] | None:
    """Find where to look, cheaply, on a quarter-size image.

    Returns boxes in full-resolution coordinates, or None when the coarse
    pass is not worth running. Most of a sheet is unchanged; this is what
    keeps an A1 comparison inside its four-second budget.
    """
    height, width = new_binary.shape[:2]
    if width * height < config.coarse_minimum_px or config.coarse_scale >= 1.0:
        return None

    scale = config.coarse_scale
    small_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    old_small = cv2.resize(old_binary, small_size, interpolation=cv2.INTER_AREA)
    new_small = cv2.resize(new_binary, small_size, interpolation=cv2.INTER_AREA)
    old_small = (old_small > 0).astype(np.uint8) * 255
    new_small = (new_small > 0).astype(np.uint8) * 255

    removed, added = tolerant_difference(old_small, new_small, max(tolerance_px * scale, 1.0))
    coarse = cv2.bitwise_or(removed, added)
    # Grow each candidate so the full-resolution pass sees the whole change.
    coarse = cv2.dilate(coarse, np.ones((3, 3), np.uint8), iterations=2)

    boxes: list[tuple[int, int, int, int, float]] = []
    # The crop's own distance transform can only see ink inside the crop, so
    # the margin has to exceed the matching tolerance or every candidate
    # reports a false unmatched fringe along its edges.
    margin = max(8, round(tolerance_px * 3))
    for x, y, box_width, box_height, _area in _components(coarse, 1.0):
        full_x = max(0, int(x / scale) - margin)
        full_y = max(0, int(y / scale) - margin)
        full_width = min(width - full_x, int(box_width / scale) + 2 * margin)
        full_height = min(height - full_y, int(box_height / scale) + 2 * margin)
        if full_width <= 0 or full_height <= 0:
            continue
        boxes.append((full_x, full_y, full_width, full_height, float(full_width * full_height)))

    if len(boxes) > 5000:
        # Thousands of candidates means the sheets barely correspond; the
        # full-resolution pass over the whole image is cheaper and honest.
        return None
    return boxes


def _record(region: RasterRegion, tolerance: ResolvedTolerance) -> ChangeRecord:
    area_m2 = area_px_to_site_m2(region.pixel_area, tolerance)
    if area_m2 is not None and area_m2 >= 0.01:
        size = f"{area_m2:.2f} m² of ink"
    else:
        paper = tolerance.px_to_paper_mm(region.bbox.w)
        size = f"{paper:.1f} mm wide on paper"

    verb = {
        ChangeKind.ADDED: "Something was added here",
        ChangeKind.REMOVED: "Something was removed here",
        ChangeKind.MODIFIED: "The drawing changed here",
    }[region.kind]

    return ChangeRecord(
        kind=region.kind,
        bbox=region.bbox,
        streams=[Stream.RASTER],
        description=f"{verb} ({size})",
        confidence=0.55,
        detail={"pixel_area": region.pixel_area},
    )
