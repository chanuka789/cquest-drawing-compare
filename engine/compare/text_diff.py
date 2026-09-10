"""Stage 6: read what a change region actually says.

Ink area is a terrible measure of how much a change matters. Editing
``3200`` to ``3400`` moves a few dozen pixels and changes where a wall
goes; restyling a hatch moves tens of thousands and changes nothing. So
every region is checked against the two sheets' text layers, and when the
words inside it differ, the change is reported in words rather than as an
area - and it is never filed as cosmetic.

This uses the text layer, so it says nothing about a scanned sheet. That
is the honest answer: no text was read, so no claim is made about text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.compare.cluster import Box
from engine.core.enums import TextChangeKind
from engine.extract.text_extractor import PageText, TextItem

#: A dimension: digits, optionally grouped or decimal, optionally with a
#: unit. Deliberately strict - a drawing is full of numbers that are not
#: dimensions, and calling a door tag a dimension helps nobody.
_DIMENSION = re.compile(r"^[<>~]?\s*\d{1,3}(?:[ ,]?\d{3})*(?:[.,]\d+)?\s*(?:mm|cm|m|MM|M)?$")

#: A tag or reference: a short mixed alphanumeric token such as D-04 or C12.
_TAG = re.compile(r"^[A-Za-z]{1,3}[-/ ]?\d{1,4}[A-Za-z]?$")


@dataclass(slots=True)
class TextChange:
    """What one region used to say, and what it says now."""

    kind: TextChangeKind
    old_text: str
    new_text: str

    @property
    def is_empty(self) -> bool:
        return not self.old_text and not self.new_text


def classify_text(old_text: str, new_text: str) -> TextChangeKind:
    """Dimension, tag, or note.

    Judged on whichever side actually has text, so a deleted dimension is
    still a dimension change rather than being demoted to a note.
    """
    for candidate in (new_text, old_text):
        stripped = candidate.strip()
        if not stripped:
            continue
        if _DIMENSION.match(stripped):
            return TextChangeKind.DIMENSION
        if _TAG.match(stripped):
            return TextChangeKind.TAG
    return TextChangeKind.NOTE


def _item_box_px(
    item: TextItem,
    page: PageText,
    width_px: int,
    height_px: int,
) -> Box:
    """One text run's box on the raster grid, with y counted from the top."""
    box = page.box
    x = item.fx * width_px
    # fy is 0 at the bottom of the page; raster rows start at the top.
    top = (1.0 - item.fy - (item.height / box.height if box.height else 0.0)) * height_px
    w = (item.width / box.width if box.width else 0.0) * width_px
    h = (item.height / box.height if box.height else 0.0) * height_px
    return (int(x), int(top), max(1, int(w)), max(1, int(h)))


def _overlaps(a: Box, b: Box, pad: int) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (
        ax - pad > bx + bw or bx - pad > ax + aw or ay - pad > by + bh or by - pad > ay + ah
    )


def text_in_box(
    page: PageText | None,
    box: Box,
    size_px: tuple[int, int],
    pad: int,
) -> str:
    """The text of *page* that falls inside *box*, in reading order.

    *size_px* is the (width, height) of the raster that *box* refers to.
    """
    if page is None or page.is_empty:
        return ""
    width_px, height_px = size_px
    hits: list[tuple[float, float, str]] = []
    for item in page.items:
        clean = item.clean
        if not clean:
            continue
        if _overlaps(_item_box_px(item, page, width_px, height_px), box, pad):
            hits.append((-item.fy, item.fx, clean))
    hits.sort()
    return " ".join(text for _, _, text in hits)


def text_change_for(
    box: Box,
    old_page: PageText | None,
    new_page: PageText | None,
    new_size: tuple[int, int],
    old_size: tuple[int, int],
    pad: int,
    *,
    old_box: Box | None = None,
) -> TextChange | None:
    """What the region says on each sheet, when the two disagree.

    *box* is on the new sheet's grid; *old_box* is the same region mapped
    back onto the old sheet's grid by undoing the alignment. Returns None
    when neither sheet has text there, or when the text is identical - in
    which case whatever changed was not the words.
    """
    old_text = text_in_box(old_page, old_box if old_box is not None else box, old_size, pad)
    new_text = text_in_box(new_page, box, new_size, pad)
    if not old_text and not new_text:
        return None
    if old_text == new_text:
        return None
    return TextChange(
        kind=classify_text(old_text, new_text),
        old_text=old_text,
        new_text=new_text,
    )
