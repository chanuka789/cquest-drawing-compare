"""Stage 6, step one: which ink is new, and which ink is gone.

The old sheet is warped onto the new sheet's pixel grid with the transform
Phase 4 fitted, both sheets are reduced to ink masks, and the two masks are
subtracted - each one dilated by the registration tolerance first, so that a
stroke which merely drifted by a fraction of a millimetre does not read as a
removal plus an addition.

Nothing here clusters or classifies. It answers only "where is the ink
different", and it refuses to answer at all when the inputs disagree about
the sheet size, because a confident wrong diff is worse than no diff.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.units import mm_to_px
from engine.compare.types import CompareConfig

#: Pixels darker than this count as ink. Matches the renderer's own
#: threshold so coverage estimates and diffs agree about what ink is.
INK_THRESHOLD = 200


@dataclass(slots=True)
class DiffMasks:
    """The two difference masks, on the new sheet's pixel grid.

    Both are ``uint8`` with 255 where the difference is. ``valid`` marks the
    pixels where the warped old sheet actually covered the new one; outside
    it no honest comparison is possible, so both masks are already zero
    there.
    """

    added: NDArray[np.uint8]
    removed: NDArray[np.uint8]
    valid: NDArray[np.uint8]
    width: int
    height: int

    @property
    def added_px(self) -> int:
        return int(np.count_nonzero(self.added))

    @property
    def removed_px(self) -> int:
        return int(np.count_nonzero(self.removed))


def ink_mask(gray: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Ink as 255, paper as 0.

    A fixed threshold rather than Otsu: Otsu re-centres itself on whatever
    is in the image, so a mostly-empty sheet and a dense one would get
    different definitions of ink and their diff would be meaningless.
    """
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_RGB2GRAY)
    mask = np.zeros(gray.shape[:2], dtype=np.uint8)
    mask[gray < INK_THRESHOLD] = 255
    return mask


def _disc(radius_px: int) -> NDArray[np.uint8]:
    size = max(1, 2 * radius_px + 1)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))


def tolerance_radius_px(config: CompareConfig) -> int:
    """The tolerance in pixels, never below one pixel of antialiasing."""
    return max(1, round(mm_to_px(config.tolerance_mm, config.dpi)))


def warp_old_onto_new(
    old_gray: NDArray[np.uint8],
    matrix: NDArray[np.float64] | None,
    new_shape: tuple[int, int],
) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
    """Warp the old sheet onto the new sheet's grid.

    Returns the warped grayscale and the coverage mask. *matrix* is the
    row-major 3x3 that Phase 4 fitted, mapping old pixels to new pixels;
    ``None`` means the sheets are already on the same grid.

    Paper (255) is used as the border value so that uncovered area reads as
    blank rather than as ink, and the coverage mask is warped alongside so
    the caller can tell blank-because-uncovered from blank-because-empty.
    """
    new_h, new_w = new_shape
    old_h, old_w = old_gray.shape[:2]
    coverage = np.full((old_h, old_w), 255, dtype=np.uint8)

    if matrix is None:
        if (old_h, old_w) == (new_h, new_w):
            return old_gray.copy(), coverage
        warped = cv2.resize(old_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        return warped, np.full((new_h, new_w), 255, dtype=np.uint8)

    affine = np.asarray(matrix, dtype=np.float64)[:2, :]
    warped = cv2.warpAffine(
        old_gray,
        affine,
        (new_w, new_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    warped_coverage = cv2.warpAffine(
        coverage,
        affine,
        (new_w, new_h),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return warped, warped_coverage


def _edge_trimmed(valid: NDArray[np.uint8], config: CompareConfig) -> NDArray[np.uint8]:
    """Pull the valid area in from its own edge.

    Warping leaves a ragged, partially-interpolated fringe wherever the old
    sheet's border landed. Eroding the coverage mask by the edge margin
    keeps that fringe from reading as a change all the way round the sheet.
    """
    margin_px = round(mm_to_px(config.edge_margin_mm, config.dpi))
    if margin_px <= 0:
        return valid
    return cv2.erode(valid, _disc(margin_px), iterations=1)


def diff_masks(
    old_gray: NDArray[np.uint8],
    new_gray: NDArray[np.uint8],
    matrix: NDArray[np.float64] | None,
    config: CompareConfig,
    *,
    exclusion_mask: NDArray[np.uint8] | None = None,
) -> DiffMasks:
    """Ink that appeared and ink that vanished, on the new sheet's grid.

    *exclusion_mask* marks zones to ignore (255 = ignore) - the title block
    and any revision stamp, which change on every reissue and would
    otherwise drown the real changes.
    """
    new_h, new_w = new_gray.shape[:2]
    warped_old, coverage = warp_old_onto_new(old_gray, matrix, (new_h, new_w))

    valid = _edge_trimmed(coverage, config)
    if exclusion_mask is not None:
        valid = cv2.bitwise_and(valid, cv2.bitwise_not(exclusion_mask))

    old_ink = cv2.bitwise_and(ink_mask(warped_old), valid)
    new_ink = cv2.bitwise_and(ink_mask(new_gray), valid)

    # Dilating the *other* side by the tolerance is what forgives drift: a
    # stroke counts as still there if any ink sits within tolerance of it.
    kernel = _disc(tolerance_radius_px(config))
    old_near = cv2.dilate(old_ink, kernel, iterations=1)
    new_near = cv2.dilate(new_ink, kernel, iterations=1)

    added = cv2.bitwise_and(new_ink, cv2.bitwise_not(old_near))
    removed = cv2.bitwise_and(old_ink, cv2.bitwise_not(new_near))

    return DiffMasks(
        added=added,
        removed=removed,
        valid=valid,
        width=new_w,
        height=new_h,
    )
