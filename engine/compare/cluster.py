"""Stage 6, step two: turn scattered different pixels into readable regions.

A raster diff produces tens of thousands of loose pixels. A user needs a
list they can walk. So nearby difference pixels are closed into blobs,
blobs become boxes, boxes that overlap or nearly touch are merged, and
anything below the noise floor is dropped.

The merge distance and the noise floor are given in millimetres on paper,
never in pixels, so they mean the same thing at any comparison DPI.
"""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.units import mm_to_px
from engine.compare.raster_diff import DiffMasks
from engine.compare.types import CompareConfig

#: A box, as x, y, width, height on the new sheet's pixel grid.
Box = tuple[int, int, int, int]


def _close_kernel(config: CompareConfig) -> NDArray[np.uint8]:
    """Bridge gaps up to half the merge distance before labelling.

    Half, because closing joins across the kernel from both sides, so a
    radius of half the gap already spans the whole gap.
    """
    radius = max(1, round(mm_to_px(config.merge_gap_mm, config.dpi) / 2))
    size = 2 * radius + 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))


def _boxes_overlap(a: Box, b: Box, pad: int) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (
        ax - pad > bx + bw or bx - pad > ax + aw or ay - pad > by + bh or by - pad > ay + ah
    )


def _union(a: Box, b: Box) -> Box:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0 = min(ax, bx)
    y0 = min(ay, by)
    x1 = max(ax + aw, bx + bw)
    y1 = max(ay + ah, by + bh)
    return (x0, y0, x1 - x0, y1 - y0)


def merge_boxes(boxes: list[Box], pad: int) -> list[Box]:
    """Repeatedly union boxes that touch, until nothing else merges.

    Quadratic in the number of boxes, which is fine: the noise floor has
    already cut the count to something a person could scroll through.
    """
    result = list(boxes)
    merged = True
    while merged:
        merged = False
        output: list[Box] = []
        while result:
            current = result.pop()
            hit = True
            while hit:
                hit = False
                for index, other in enumerate(result):
                    if _boxes_overlap(current, other, pad):
                        current = _union(current, other)
                        result.pop(index)
                        hit = True
                        merged = True
                        break
            output.append(current)
        result = output
    return result


def cluster_regions(masks: DiffMasks, config: CompareConfig) -> list[Box]:
    """Boxes around every meaningful cluster of changed ink.

    Both difference masks are clustered together, so that a stroke which
    moved a little - removed here, added just beside - becomes one region
    the user reads once, rather than two they have to correlate by eye.
    """
    combined = cv2.bitwise_or(masks.added, masks.removed)
    if not np.any(combined):
        return []

    closed = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, _close_kernel(config))
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)

    min_area_px = mm_to_px(1.0, config.dpi) ** 2 * config.min_area_mm2
    boxes: list[Box] = []
    for label in range(1, count):
        x, y, w, h, _area = stats[label]
        # Area of the closed blob overstates the ink; measure the real
        # changed ink inside the box instead, so closing cannot invent one.
        window = combined[y : y + h, x : x + w]
        ink = int(np.count_nonzero(window))
        if ink < min_area_px:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))

    pad = max(1, round(mm_to_px(config.merge_gap_mm, config.dpi)))
    return merge_boxes(boxes, pad)


def box_ink(mask: NDArray[np.uint8], box: Box) -> int:
    """Changed pixels of *mask* inside *box*."""
    x, y, w, h = box
    return int(np.count_nonzero(mask[y : y + h, x : x + w]))


def sort_boxes(boxes: list[Box]) -> list[Box]:
    """Reading order: down the sheet, then across.

    Users walk a drawing the way they read, and a change list that jumps
    around the sheet is a change list nobody finishes.
    """
    return sorted(boxes, key=lambda box: (box[1], box[0]))
