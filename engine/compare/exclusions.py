"""Stage 5, used by Stage 6: the parts of a sheet that must not be compared.

The title block changes on every single reissue - the revision letter, the
date, the signature, sometimes the whole revision history table. Comparing
it would put a change on every sheet in the set and bury the one that
matters. So it is masked out before the diff runs.

The mask is built from the title block zone Phase 2 already detects, so a
sheet whose title block sits somewhere unusual is handled by fixing the
sheet profile rather than by adding a special case here.

The zone is only masked when it was actually found. A sheet with no
readable text gets no mask at all: hiding an arbitrary corner of a scanned
drawing would silently drop real changes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from engine.extract.text_extractor import PageText
from engine.titleblock.zone_detector import ZoneName, best_zone

#: Grow the detected zone by this fraction of the sheet, so the border
#: rules and the revision cloud beside the block are covered too.
_ZONE_PADDING = 0.01


@dataclass(slots=True)
class ExclusionZone:
    """One masked rectangle, in fractions of the sheet with y from the top."""

    name: str
    x_from: float
    y_from: float
    x_to: float
    y_to: float
    reason: str


def title_block_zone(page: PageText | None) -> ExclusionZone | None:
    """The title block as a top-left-origin fractional rectangle.

    Returns None when no title block could be located, or when the only
    candidate was the whole sheet - masking everything would leave nothing
    to compare.
    """
    if page is None or page.is_empty:
        return None
    zone = best_zone(page)
    if zone is None or zone.name is ZoneName.WHOLE_SHEET:
        return None

    # Zone bounds measure y from the bottom of the page; raster rows count
    # from the top, so the two y bounds swap and invert.
    x_from = max(0.0, zone.x_from - _ZONE_PADDING)
    x_to = min(1.0, zone.x_to + _ZONE_PADDING)
    y_from = max(0.0, (1.0 - zone.y_to) - _ZONE_PADDING)
    y_to = min(1.0, (1.0 - zone.y_from) + _ZONE_PADDING)

    return ExclusionZone(
        name=str(zone.name),
        x_from=x_from,
        y_from=y_from,
        x_to=x_to,
        y_to=y_to,
        reason="Title block — the revision letter, date and signature change on every issue.",
    )


def build_mask(
    zones: list[ExclusionZone],
    width_px: int,
    height_px: int,
) -> NDArray[np.uint8] | None:
    """A 255-where-ignored mask on the given pixel grid, or None if empty."""
    if not zones or width_px <= 0 or height_px <= 0:
        return None
    mask = np.zeros((height_px, width_px), dtype=np.uint8)
    painted = False
    for zone in zones:
        x0 = round(zone.x_from * width_px)
        x1 = round(zone.x_to * width_px)
        y0 = round(zone.y_from * height_px)
        y1 = round(zone.y_to * height_px)
        x0, x1 = max(0, min(x0, x1)), min(width_px, max(x0, x1))
        y0, y1 = max(0, min(y0, y1)), min(height_px, max(y0, y1))
        if x1 <= x0 or y1 <= y0:
            continue
        mask[y0:y1, x0:x1] = 255
        painted = True
    return mask if painted else None
