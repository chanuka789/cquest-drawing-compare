"""Pull text off a PDF page, with coordinates.

Everything downstream — title block reading, drawing numbers, revisions,
scales — depends on knowing *where* a piece of text sits, not just what it
says. A label means nothing on its own; `Drawing No.` matters because of what
is directly below it.

Two facts about real drawings that this module exists to handle:

* **The page box is not always at the origin.** The Lami Architects fixture
  runs from (-1192, -842) to (1192, 842). Coordinates are therefore reported
  both raw and normalised to a 0..1 fraction of the page box, and every zone
  test uses the fraction.
* **Font size is not readable directly.** pdfium reports a nominal size of 1.0
  with the real scale folded into the text matrix, so the effective size is
  derived from the height of the glyph box. That is what matters anyway: in a
  title block the value is set larger than its label.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pypdfium2 as pdfium
from loguru import logger

from engine.utils.pdf_runtime import open_document, pdfium_access

POINTS_PER_MM = 72.0 / 25.4


@dataclass(frozen=True, slots=True)
class PageBox:
    """The page's own coordinate box. Never assume it starts at (0, 0)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def fraction_x(self, x: float) -> float:
        return (x - self.x0) / self.width if self.width else 0.0

    def fraction_y(self, y: float) -> float:
        """0.0 at the bottom of the page, 1.0 at the top."""
        return (y - self.y0) / self.height if self.height else 0.0


@dataclass(frozen=True, slots=True)
class TextItem:
    """One run of text, with where it sits on the page."""

    text: str
    x: float
    y: float
    width: float
    height: float
    #: Effective size in points, derived from the glyph box height.
    font_size: float
    #: Position as a fraction of the page box, so zones work on any page.
    fx: float
    fy: float
    #: Height as a fraction of the page box, so line spacing can be compared
    #: without knowing the sheet size.
    fh: float = 0.0

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height

    @property
    def centre_y(self) -> float:
        return self.y + self.height / 2

    @property
    def clean(self) -> str:
        """Collapsed whitespace, for matching."""
        return " ".join(self.text.split())


@dataclass(slots=True)
class PageText:
    """Everything readable on one page."""

    page_index: int
    box: PageBox
    items: list[TextItem] = field(default_factory=list)
    rotation: int = 0

    @property
    def char_count(self) -> int:
        return sum(len(item.text) for item in self.items)

    @property
    def is_empty(self) -> bool:
        return not self.items

    def in_zone(
        self,
        x_from: float = 0.0,
        x_to: float = 1.0,
        y_from: float = 0.0,
        y_to: float = 1.0,
    ) -> list[TextItem]:
        """Items whose origin falls inside a fractional region of the page."""
        return [
            item for item in self.items if x_from <= item.fx <= x_to and y_from <= item.fy <= y_to
        ]

    def joined(self) -> str:
        """All text in reading order, for a whole-sheet regex sweep."""
        ordered = sorted(self.items, key=lambda item: (-item.fy, item.fx))
        return "\n".join(item.clean for item in ordered if item.clean)


def _page_box(page: pdfium.PdfPage) -> PageBox:
    """The media box, falling back to the page size at the origin."""
    try:
        box = page.get_mediabox()
        if box and len(box) == 4:
            x0, y0, x1, y1 = box
            if x1 > x0 and y1 > y0:
                return PageBox(float(x0), float(y0), float(x1), float(y1))
    except (pdfium.PdfiumError, TypeError, ValueError):
        pass

    width, height = page.get_size()
    return PageBox(0.0, 0.0, float(width), float(height))


#: Two runs merge when the gap between them is under this multiple of the
#: text height. A word space is far smaller than the gap between a title
#: block label and its value, so this joins broken words without gluing
#: neighbouring cells together.
MERGE_GAP_RATIO = 0.6
#: How far apart two runs' baselines may be and still count as one line.
MERGE_BASELINE_RATIO = 0.3


def merge_runs(items: list[TextItem]) -> list[TextItem]:
    """Join text runs that pdfium split in the middle of a phrase.

    pdfium reports "GROUND FLOOR REFLECTED CEILING" and "PLAN" as two runs, so
    a drawing title comes back missing its last word. Runs on the same
    baseline separated by no more than a word space are stitched back
    together; anything further apart is left alone, because a wide gap is what
    separates a title block label from its value.
    """
    if len(items) < 2:
        return items

    ordered = sorted(items, key=lambda item: (-item.y, item.x))
    merged: list[TextItem] = []

    for item in ordered:
        if not merged:
            merged.append(item)
            continue

        previous = merged[-1]
        height = max(previous.height, item.height)
        same_line = abs(item.y - previous.y) <= height * MERGE_BASELINE_RATIO
        gap = item.x - previous.right

        if same_line and -height * 0.2 <= gap <= height * MERGE_GAP_RATIO:
            joiner = "" if gap <= 0 else " "
            text = f"{previous.text.rstrip()}{joiner}{item.text.lstrip()}"
            right = max(previous.right, item.right)
            merged[-1] = TextItem(
                text=text,
                x=previous.x,
                y=min(previous.y, item.y),
                width=right - previous.x,
                height=height,
                font_size=previous.font_size,
                fx=previous.fx,
                fy=previous.fy,
                fh=max(previous.fh, item.fh),
            )
        else:
            merged.append(item)

    return merged


def extract_page_text(page: pdfium.PdfPage, page_index: int = 0) -> PageText:
    """Read every text run on *page* with its position.

    Never raises for a page that cannot be read; an empty result means there
    is no text layer, which is itself a useful answer (the sheet is scanned).
    """
    # pdfium is not thread-safe, so every read of an open page is guarded.
    # The lock is re-entrant, so this is free when the caller already holds it.
    with pdfium_access():
        return _read_page_text(page, page_index)


def _read_page_text(page: pdfium.PdfPage, page_index: int) -> PageText:
    box = _page_box(page)
    result = PageText(page_index=page_index, box=box, rotation=page.get_rotation())

    try:
        textpage = page.get_textpage()
    except pdfium.PdfiumError as exc:
        logger.debug("No text layer on page {}: {}", page_index, exc)
        return result

    try:
        count = textpage.count_rects()
    except pdfium.PdfiumError:
        return result

    for index in range(count):
        try:
            left, bottom, right, top = textpage.get_rect(index)
            text = textpage.get_text_bounded(left, bottom, right, top)
        except pdfium.PdfiumError:
            continue

        if not text or not text.strip():
            continue

        height = top - bottom
        result.items.append(
            TextItem(
                text=text,
                x=left,
                y=bottom,
                width=right - left,
                height=height,
                # The glyph box is a little taller than the nominal point
                # size because of ascenders and descenders.
                font_size=round(height * 0.85, 2),
                fx=box.fraction_x(left),
                fy=box.fraction_y(bottom),
                fh=height / box.height if box.height else 0.0,
            )
        )

    result.items = merge_runs(result.items)
    return result


def extract_document_text(path: str, pages: list[int] | None = None) -> dict[int, PageText]:
    """Read text from a whole PDF, or just the pages listed."""
    output: dict[int, PageText] = {}
    try:
        with open_document(path) as document:
            indices = pages if pages is not None else range(len(document))
            for index in indices:
                if 0 <= index < len(document):
                    output[index] = extract_page_text(document[index], index)
    except pdfium.PdfiumError as exc:
        logger.debug("Could not read text from {}: {}", path, exc)
    return output
