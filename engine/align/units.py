"""Unit conversions for Phase 4.

Phase 4 rule: tolerances and errors are expressed in millimetres on paper AND
in real-world millimetres at drawing scale; pixels are internal. These helpers
are the only place those conversions happen, so a wrong conversion cannot
silently spread.
"""

from __future__ import annotations

import math

#: Millimetres per inch, exact.
MM_PER_INCH = 25.4


def px_per_mm(dpi: float) -> float:
    """Pixels per millimetre at a given DPI."""
    return dpi / MM_PER_INCH


def mm_to_px(millimetres: float, dpi: float) -> float:
    return millimetres * px_per_mm(dpi)


def px_to_mm(pixels: float, dpi: float) -> float:
    return pixels / px_per_mm(dpi)


def site_mm(paper_mm: float, scale_denominator: int | None) -> float | None:
    """Real-world millimetres represented by *paper_mm* at a drawing scale.

    A 0.6 mm error on paper at 1:100 is 60 mm on site. Without a known scale
    the answer is None — never pretend the conversion happened.
    """
    if not scale_denominator:
        return None
    return paper_mm * scale_denominator


def expected_content_scale(
    old_scale_denominator: int | None,
    new_scale_denominator: int | None,
    old_area_mm2: float,
    new_area_mm2: float,
) -> float | None:
    """Metadata prior for the content scale between two issues (B4, method 6).

    ``expected = (old_den / new_den) * sqrt(new_area / old_area)`` — drawing
    scale change times paper size change. The plan's worked example (1:100 A1
    reissued at 1:50 one A-size smaller) lands at 1.414 with this convention.
    Without both scale denominators there is no prior.
    """
    if not old_scale_denominator or not new_scale_denominator:
        return None
    if old_area_mm2 <= 0 or new_area_mm2 <= 0:
        return None
    scale_ratio = old_scale_denominator / new_scale_denominator
    paper_ratio = math.sqrt(new_area_mm2 / old_area_mm2)
    return scale_ratio * paper_ratio
