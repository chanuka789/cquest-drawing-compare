"""Stage 6, step three: say what kind of change each region is.

Type comes from the balance of new and vanished ink inside the region:
only new ink is an addition, only vanished ink is a removal, both is a
modification. A removal that turns up again elsewhere on the sheet, at the
same shape, is a move rather than a deletion plus an unrelated addition.

Severity ranks by how much of the *drawing* changed, not by pixel count,
and it never implies a cost. Stage 8 links changes to money; this module
only says how big the change is and how sure it is of what it saw.
"""

from __future__ import annotations

from collections.abc import Callable

import cv2
import numpy as np
from numpy.typing import NDArray

from engine.align.units import mm_to_px, px_to_mm, site_mm
from engine.compare.cluster import Box, box_ink
from engine.compare.raster_diff import DiffMasks
from engine.compare.text_diff import TextChange
from engine.compare.types import ChangeRegion, CompareConfig
from engine.core.enums import ChangeType, Severity, TextChangeKind

#: Below this share of the smaller side, a region with both kinds of ink is
#: really just one of them with a little noise attached.
_MIXED_SHARE = 0.15

#: A move's destination must carry at least this share of the vanished
#: ink, or the "move" is really a deletion that happens to rhyme with
#: something else on the sheet.
_MOVE_INK_SHARE = 0.5

#: Severity bands, as a share of the sheet's area on paper.
_MAJOR_AREA_MM2 = 2500.0  # ~50 mm x 50 mm
_MINOR_AREA_MM2 = 200.0


def _classify(added: int, removed: int) -> ChangeType:
    """Additions, removals, or both."""
    total = added + removed
    if total == 0:
        return ChangeType.COSMETIC
    if removed == 0 or removed / total < _MIXED_SHARE:
        return ChangeType.ADDED
    if added == 0 or added / total < _MIXED_SHARE:
        return ChangeType.REMOVED
    return ChangeType.MODIFIED


def _severity(
    area_mm2: float,
    change_type: ChangeType,
    *,
    is_cosmetic: bool,
    text: TextChange | None = None,
) -> Severity:
    """Rank by changed area on paper, with removals treated more seriously.

    A removal is the change most likely to be missed on site - something
    that was built to the old sheet and is no longer wanted - so it is
    never ranked below MINOR.

    Text outranks area entirely. A dimension that changed is critical no
    matter how few pixels it moved; ranking ``3200`` becoming ``3400`` as
    trivial because it is small is precisely the failure this app exists
    to prevent.
    """
    if text is not None:
        if text.kind is TextChangeKind.DIMENSION:
            return Severity.CRITICAL
        if text.kind is TextChangeKind.TAG:
            return Severity.MAJOR
        return Severity.MINOR
    if is_cosmetic:
        return Severity.TRIVIAL
    if area_mm2 >= _MAJOR_AREA_MM2:
        return Severity.CRITICAL if change_type is ChangeType.REMOVED else Severity.MAJOR
    if area_mm2 >= _MINOR_AREA_MM2:
        return Severity.MAJOR if change_type is ChangeType.REMOVED else Severity.MINOR
    if change_type is ChangeType.REMOVED:
        return Severity.MINOR
    return Severity.MINOR


def _crop(mask: NDArray[np.uint8], box: Box) -> NDArray[np.uint8]:
    x, y, w, h = box
    return mask[y : y + h, x : x + w]


def _find_move(
    box: Box,
    masks: DiffMasks,
    config: CompareConfig,
) -> tuple[Box, float] | None:
    """Did this vanished shape reappear nearby?

    The removed ink in *box* is used as a template and matched against the
    added-ink mask within the search radius. A strong, unambiguous peak
    means the same content is now somewhere else.

    Returns the destination box and the distance moved in pixels, or None.
    """
    template = _crop(masks.removed, box)
    h, w = template.shape[:2]
    template_ink = int(np.count_nonzero(template))
    if h < 3 or w < 3 or template_ink == 0:
        return None

    search_px = round(mm_to_px(config.move_search_mm, config.dpi))
    x, y, _, _ = box
    x0 = max(0, x - search_px)
    y0 = max(0, y - search_px)
    x1 = min(masks.width, x + w + search_px)
    y1 = min(masks.height, y + h + search_px)
    window = masks.added[y0:y1, x0:x1]
    if window.shape[0] < h or window.shape[1] < w:
        return None

    # Nothing arrived anywhere near here, so nothing moved. Checked before
    # matching because a normalised correlation against an empty window is
    # a division by zero, and OpenCV reports the result as a perfect score.
    if not np.any(window):
        return None

    scores = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
    scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
    _, best, _, location = cv2.minMaxLoc(scores)
    if best < config.move_correlation:
        return None

    dest = (x0 + int(location[0]), y0 + int(location[1]), w, h)

    # The destination must actually hold a comparable amount of new ink.
    # Correlation alone will happily match a shape against a faint echo of
    # itself, and calling that a move hides a real deletion.
    dest_ink = box_ink(masks.added, dest)
    if dest_ink < template_ink * _MOVE_INK_SHARE:
        return None

    distance = float(np.hypot(dest[0] - x, dest[1] - y))
    # A "move" of almost nothing is drift the tolerance should have eaten.
    if distance < mm_to_px(config.merge_gap_mm, config.dpi):
        return None
    return dest, distance


def _explain(
    change_type: ChangeType,
    area_mm2: float,
    area_site_mm2: float | None,
    moved_by_mm: float | None,
    moved_by_site_mm: float | None,
    *,
    is_cosmetic: bool,
    text: TextChange | None = None,
) -> str:
    """One sentence, in the user's units, with no jargon and no guesses."""
    if text is not None:
        label = {
            TextChangeKind.DIMENSION: "A dimension",
            TextChangeKind.TAG: "A tag",
            TextChangeKind.NOTE: "Text",
        }[text.kind]
        if not text.old_text:
            return f"{label} was added: “{text.new_text}”."
        if not text.new_text:
            return f"{label} was deleted: “{text.old_text}”."
        return f"{label} changed from “{text.old_text}” to “{text.new_text}”."

    size = f"{area_mm2:.1f} mm² on paper"
    if area_site_mm2 is not None:
        size += f" ({area_site_mm2 / 1_000_000:.2f} m² at drawing scale)"

    if change_type is ChangeType.ADDED:
        sentence = f"New content on the current sheet, covering {size}."
    elif change_type is ChangeType.REMOVED:
        sentence = f"Content on the previous sheet that is no longer there, covering {size}."
    elif change_type is ChangeType.MOVED:
        distance = f"{moved_by_mm:.1f} mm on paper" if moved_by_mm is not None else "some distance"
        if moved_by_site_mm is not None:
            distance += f" ({moved_by_site_mm / 1000:.2f} m at drawing scale)"
        sentence = f"The same content, moved {distance}."
    else:
        sentence = f"Content that was redrawn, covering {size}."

    if is_cosmetic:
        sentence += " Small enough to be presentation only — check before relying on it."
    return sentence


def build_regions(
    boxes: list[Box],
    masks: DiffMasks,
    config: CompareConfig,
    scale_denominator: int | None,
    text_lookup: Callable[[Box], TextChange | None] | None = None,
) -> list[ChangeRegion]:
    """Type, rank and describe every clustered box.

    *text_lookup* reads what a box says on each sheet. It is optional so
    that the raster path stays testable on its own, and returns None for a
    scanned sheet, where there is no text layer to read.
    """
    px_mm = mm_to_px(1.0, config.dpi)
    area_per_px_mm2 = 1.0 / (px_mm * px_mm)

    # A box already claimed as the destination of a move must not also be
    # reported as an unexplained addition.
    move_destinations: set[Box] = set()
    regions: list[ChangeRegion] = []

    for index, box in enumerate(boxes):
        added = box_ink(masks.added, box)
        removed = box_ink(masks.removed, box)
        change_type = _classify(added, removed)

        moved_from: Box | None = None
        moved_by_mm: float | None = None
        moved_by_site_mm: float | None = None

        if change_type is ChangeType.REMOVED:
            found = _find_move(box, masks, config)
            if found is not None:
                destination, distance_px = found
                change_type = ChangeType.MOVED
                moved_from = box
                move_destinations.add(destination)
                moved_by_mm = px_to_mm(distance_px, config.dpi)
                moved_by_site_mm = site_mm(moved_by_mm, scale_denominator)

        changed_px = added + removed
        area_mm2 = changed_px * area_per_px_mm2
        area_site_mm2 = (
            area_mm2 * scale_denominator * scale_denominator if scale_denominator else None
        )
        text = text_lookup(box) if text_lookup is not None else None
        # Words that changed are never cosmetic, however little ink moved.
        is_cosmetic = text is None and area_mm2 <= config.cosmetic_area_mm2

        x, y, w, h = box
        regions.append(
            ChangeRegion(
                index=index,
                change_type=change_type,
                severity=_severity(area_mm2, change_type, is_cosmetic=is_cosmetic, text=text),
                bbox_px=(x, y, w, h),
                bbox_mm=(
                    px_to_mm(x, config.dpi),
                    px_to_mm(y, config.dpi),
                    px_to_mm(w, config.dpi),
                    px_to_mm(h, config.dpi),
                ),
                area_mm2=area_mm2,
                area_site_mm2=area_site_mm2,
                added_px=added,
                removed_px=removed,
                is_cosmetic=is_cosmetic,
                moved_from_px=moved_from,
                moved_by_mm=moved_by_mm,
                moved_by_site_mm=moved_by_site_mm,
                text_kind=str(text.kind) if text is not None else None,
                old_text=text.old_text if text is not None else None,
                new_text=text.new_text if text is not None else None,
                explanation=_explain(
                    change_type,
                    area_mm2,
                    area_site_mm2,
                    moved_by_mm,
                    moved_by_site_mm,
                    is_cosmetic=is_cosmetic,
                    text=text,
                ),
            )
        )

    return regions
