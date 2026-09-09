"""Tests for the grid bubble detector and matcher (Phase 4, task 4.5).

Everything is synthetic: bubbles are drawn with :func:`cv2.circle` onto a
white 2000 x 1500 canvas that stands in for a sheet rendered at ~100 DPI
(4 px per mm), so the 8-12 mm bubble band from :class:`AlignConfig` becomes
a 32-48 px radius band and a 42 px test bubble sits comfortably inside it.
The suite needs no fixtures and runs in a few seconds.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from engine.align.grid_bubbles import GridBubble, detect_bubbles, match_bubbles

WIDTH = 2000
HEIGHT = 1500
PPMM = 4.0  # 8-12 mm bubbles => 32-48 px radius band
RADIUS = 42.0
RING_THICKNESS = 6
COLS = (250, 1000, 1750)
ROWS = (250, 620, 990, 1360)
COL_LETTERS = ("A", "B", "C")
#: True centre -> label for the 3 x 4 grid (top-to-bottom, left-to-right).
KNOWN: dict[tuple[int, int], str] = {
    (x, y): f"{COL_LETTERS[c]}{r + 1}" for r, y in enumerate(ROWS) for c, x in enumerate(COLS)
}
#: Grid centres that are interior to the sheet: column 2 is far from both
#: side edges and these rows are far from the top edge (band = 15% of 1500
#: is 225 px, so y=250 is just inside, y=1360 is in the bottom band).
INTERIOR: frozenset[tuple[int, int]] = frozenset(
    {(COLS[1], ROWS[0]), (COLS[1], ROWS[1]), (COLS[1], ROWS[2])}
)
#: Off-grid stray circles for the collinearity test: each is more than
#: 2 x radius away from every grid row/column and no three of them share a
#: row or column, so no run of three can ever form.
STRAYS: tuple[tuple[int, int], ...] = ((600, 430), (550, 1150), (900, 1180), (1400, 520))
REFERENCE_RADIUS_PX = 10.0 * 200.0 / 25.4  # same formula as the module


def _canvas() -> NDArray[np.uint8]:
    """A white sheet-sized canvas."""
    return np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)


def _grid_lines(image: NDArray[np.uint8]) -> None:
    """Draw the grid rows and columns as thin grey lines across the sheet."""
    for y in ROWS:
        cv2.line(image, (0, y), (WIDTH, y), 90, 2)
    for x in COLS:
        cv2.line(image, (x, 0), (x, HEIGHT), 90, 2)


def _bubble(
    image: NDArray[np.uint8],
    centre: tuple[float, float],
    label: str = "",
    *,
    filled: bool = False,
) -> None:
    """Draw one bubble (dark ring, optional centred label) or a filled disc."""
    x, y = centre
    cv2.circle(image, (int(x), int(y)), int(RADIUS), 0, -1 if filled else RING_THICKNESS)
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.75
        (text_width, text_height), _ = cv2.getTextSize(label, font, scale, 2)
        origin = (int(x) - text_width // 2, int(y) + text_height // 2)
        cv2.putText(image, label, origin, font, scale, 0, 2, cv2.LINE_AA)


def _grid_sheet(
    *,
    bubbles: bool = True,
    filled_at: tuple[tuple[float, float], ...] = (),
    strays: tuple[tuple[float, float], ...] = (),
) -> NDArray[np.uint8]:
    """A sheet with the grid lines and, optionally, bubbles and extras."""
    image = _canvas()
    _grid_lines(image)
    if bubbles:
        for centre, label in KNOWN.items():
            if centre not in filled_at:
                _bubble(image, centre, label=label)
        for centre in filled_at:
            _bubble(image, centre, filled=True)
    for centre in strays:
        _bubble(image, centre)
    return image


def _known_centre_near(
    centre: tuple[float, float], tolerance: float = 5.0
) -> tuple[int, int] | None:
    """The true grid centre within *tolerance* of *centre*, if any."""
    x, y = centre
    nearest: tuple[float, tuple[int, int]] | None = None
    for known in KNOWN:
        distance = math.hypot(x - known[0], y - known[1])
        if distance <= tolerance and (nearest is None or distance < nearest[0]):
            nearest = (distance, known)
    return nearest[1] if nearest else None


def _label_from_known(centre: tuple[float, float]) -> str | None:
    """label_for callback: the label of the grid centre within 15 px."""
    known = _known_centre_near(centre, tolerance=15.0)
    return KNOWN[known] if known is not None else None


def test_detects_grid_of_twelve_with_positions_and_labels() -> None:
    found = detect_bubbles(_grid_sheet(), PPMM, label_for=_label_from_known)

    assert len(found) >= 10  # the plan's bar: >= 10 of the 12
    assert len({_known_centre_near(bubble.centre) for bubble in found}) == len(found)
    for bubble in found:
        known = _known_centre_near(bubble.centre)
        assert known is not None  # no junk: every centre is within 5 px of a real one
        assert bubble.label == KNOWN[known]
        assert bubble.label in KNOWN.values()
        assert 32.0 <= bubble.radius_px <= 48.0  # converted 8-12 mm band
        assert 0.0 <= bubble.interior_ink_fraction < 0.5  # outline, not a fill
    # Deterministic ordering: top-to-bottom, then left-to-right.
    assert found == sorted(found, key=lambda b: (b.centre[1], b.centre[0]))
    assert detect_bubbles(_grid_sheet(), PPMM, label_for=_label_from_known) == found


def test_no_false_positives_on_plain_grid_lines() -> None:
    assert detect_bubbles(_grid_sheet(bubbles=False), PPMM) == []


def test_blank_sheet_has_no_bubbles() -> None:
    assert detect_bubbles(_canvas(), PPMM) == []


def test_filled_circles_are_not_reported() -> None:
    filled = ((COLS[1], ROWS[1]), (COLS[2], ROWS[0]), (COLS[0], ROWS[2]))
    found = detect_bubbles(_grid_sheet(filled_at=filled), PPMM, label_for=_label_from_known)
    for bubble in found:
        assert not any(math.hypot(bubble.x - fx, bubble.y - fy) <= 5.0 for fx, fy in filled)
        known = _known_centre_near(bubble.centre)
        assert known is not None
        assert bubble.label == KNOWN[known]
    assert len(found) == 9  # the 3 filled circles were rejected, all others kept


def test_stray_circles_off_the_grid_are_dropped() -> None:
    found = detect_bubbles(_grid_sheet(strays=STRAYS), PPMM, label_for=_label_from_known)
    assert len(found) == 12
    for bubble in found:
        assert _known_centre_near(bubble.centre) is not None  # strays never appear
        assert bubble.label in KNOWN.values()


def test_interior_bubbles_kept_with_half_confidence() -> None:
    found = detect_bubbles(_grid_sheet(), PPMM, label_for=_label_from_known)
    interior_found = 0
    for bubble in found:
        known = _known_centre_near(bubble.centre)
        assert known is not None
        if known in INTERIOR:
            interior_found += 1
            assert bubble.confidence == pytest.approx(1.0 * 0.85 * 0.5, rel=1e-9)
        else:
            assert bubble.confidence == pytest.approx(1.0 * 0.85, rel=1e-9)
    assert interior_found == len(INTERIOR)  # interior group kept, not dropped


def test_detect_without_label_for_leaves_labels_empty() -> None:
    found = detect_bubbles(_grid_sheet(), PPMM)
    assert len(found) >= 10
    assert all(bubble.label == "" for bubble in found)


def test_rejects_nonsense_px_per_mm() -> None:
    with pytest.raises(ValueError, match="px_per_mm_value"):
        detect_bubbles(_grid_sheet(), 0.0)


def _bubble_pair_correspondences() -> tuple[list[GridBubble], list[GridBubble]]:
    """Old and new 3 x 4 grids; the new one is shifted and re-rings the
    bubbles at 48 px, and label B2 appears twice on the new side."""
    old = [
        GridBubble(centre=centre, radius_px=RADIUS, label=label) for centre, label in KNOWN.items()
    ]
    new = []
    for centre, label in KNOWN.items():
        shifted = (centre[0] + 40.0, centre[1] + 30.0)
        new.append(GridBubble(centre=shifted, radius_px=48.0, label=label))
        if label == "B2":
            new.append(
                GridBubble(
                    centre=(centre[0] + 100.0, centre[1] - 200.0), radius_px=48.0, label=label
                )
            )
    return old, new


def test_match_bubbles_by_label_drops_duplicates() -> None:
    old, new = _bubble_pair_correspondences()
    matched = match_bubbles(old, new)

    assert [c.label for c in matched] == sorted(c.label for c in matched)
    assert len(matched) == 11
    assert "B2" not in {c.label for c in matched}
    expected_weight = math.sqrt(RADIUS * 48.0) / REFERENCE_RADIUS_PX
    for correspondence in matched:
        original = next(centre for centre, label in KNOWN.items() if label == correspondence.label)
        assert correspondence.old_x == pytest.approx(original[0])
        assert correspondence.old_y == pytest.approx(original[1])
        assert correspondence.new_x == pytest.approx(original[0] + 40.0)
        assert correspondence.new_y == pytest.approx(original[1] + 30.0)
        assert correspondence.weight == pytest.approx(expected_weight)


def test_match_weight_grows_with_bubble_size() -> None:
    small_old = GridBubble(centre=(0.0, 0.0), radius_px=42.0, label="S")
    small_new = GridBubble(centre=(10.0, 0.0), radius_px=42.0, label="S")
    large_old = GridBubble(centre=(0.0, 100.0), radius_px=84.0, label="L")
    large_new = GridBubble(centre=(10.0, 100.0), radius_px=84.0, label="L")
    matched = match_bubbles([small_old, large_old], [small_new, large_new])
    by_label = {correspondence.label: correspondence for correspondence in matched}
    assert by_label["S"].weight == pytest.approx(42.0 / REFERENCE_RADIUS_PX)
    assert by_label["L"].weight == pytest.approx(84.0 / REFERENCE_RADIUS_PX)
    assert by_label["L"].weight > by_label["S"].weight


def test_unlabelled_bubbles_never_match() -> None:
    # The single "" bubble on each side must never pair: labels are exact
    # matches and an empty label vouches for nothing (no OCR in this
    # project). Only K9 can match.
    old = [
        GridBubble(centre=(10.0, 10.0), radius_px=RADIUS),
        GridBubble(centre=(500.0, 500.0), radius_px=RADIUS, label="K9"),
    ]
    new = [
        GridBubble(centre=(20.0, 20.0), radius_px=RADIUS),
        GridBubble(centre=(510.0, 510.0), radius_px=RADIUS, label="K9"),
    ]
    matched = match_bubbles(old, new)
    assert [c.label for c in matched] == ["K9"]
    assert len(matched) == 1


def test_detected_bubbles_never_match_without_labels() -> None:
    # Honest behaviour documented on match_bubbles: without label_for (no OCR
    # in this project) every label is "" and nothing can be paired.
    old = detect_bubbles(_grid_sheet(), PPMM)
    new = detect_bubbles(_grid_sheet(), PPMM)
    assert old
    assert match_bubbles(old, new) == []
