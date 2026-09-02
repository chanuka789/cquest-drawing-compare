"""Use the imported drawing list to identify sheets the title block could not.

This is priority 4 of the drawing-number order in the plan: after the title
block, after the sheet text, after the file name, and before asking the user.

It only ever fills a gap. A sheet that already has a number from its own title
block is never overwritten by the list, because the sheet is the primary
record and the list is a secondary one — a register can be out of date, and a
drawing cannot disagree with itself.

Two ways a match is made, both conservative:

* the file name contains a drawing number that is on the list;
* the file name reads like a title on the list, matched fuzzily and only
  accepted well above the noise floor.

Anything less certain is left unidentified for the user to resolve, because a
wrong number here silently pairs two unrelated drawings.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger
from rapidfuzz import fuzz, process

from engine.core.models import ListEntry, SheetRecord
from engine.titleblock.field_extractor import NumberSource, normalise_number

#: A title match must score at least this to be believed. Set high: a wrong
#: number pairs two unrelated drawings and nothing downstream will notice.
TITLE_MATCH_THRESHOLD = 88.0

#: Confidence recorded for a number that came from the list, not the sheet.
LIST_CONFIDENCE = 0.7

_WORDS = re.compile(r"[^A-Z0-9]+")


@dataclass(slots=True)
class ListMatchResult:
    """How many gaps the drawing list managed to fill."""

    by_number: int = 0
    by_title: int = 0

    @property
    def total(self) -> int:
        return self.by_number + self.by_title

    def summary(self) -> str:
        if not self.total:
            return "The drawing list did not identify any further sheets."
        parts = []
        if self.by_number:
            parts.append(f"{self.by_number} by drawing number")
        if self.by_title:
            parts.append(f"{self.by_title} by title")
        noun = "sheet" if self.total == 1 else "sheets"
        return f"The drawing list identified {self.total} more {noun}: {', '.join(parts)}."


def _title_key(text: str) -> str:
    """Comparison form of a title or file name: upper case, letters and digits."""
    return _WORDS.sub(" ", text.upper()).strip()


def apply_drawing_list(
    sheets: Sequence[SheetRecord], entries: Sequence[ListEntry]
) -> ListMatchResult:
    """Fill in missing drawing numbers from *entries*, in place.

    Returns what was matched, so the user can be told the list did something
    rather than wondering why the count changed.
    """
    result = ListMatchResult()
    if not entries:
        return result

    unidentified = [sheet for sheet in sheets if sheet.is_readable and not sheet.identified]
    if not unidentified:
        return result

    by_normalised = {
        entry.normalised_no or normalise_number(entry.drawing_no): entry for entry in entries
    }
    titles = {_title_key(entry.title): entry for entry in entries if entry.title}

    for sheet in unidentified:
        entry = _match_by_number(sheet, by_normalised)
        if entry is not None:
            _adopt(sheet, entry, "the file name contains a number on the drawing list")
            result.by_number += 1
            continue

        entry = _match_by_title(sheet, titles)
        if entry is not None:
            _adopt(sheet, entry, "the file name matches a title on the drawing list")
            result.by_title += 1

    if result.total:
        logger.info("Drawing list filled {} gaps: {}", result.total, result.summary())
    return result


def _match_by_number(sheet: SheetRecord, by_normalised: dict[str, ListEntry]) -> ListEntry | None:
    """A list number that appears in the file name."""
    stem = normalise_number(sheet.filename.rsplit(".", 1)[0])
    if not stem:
        return None

    # Longest first, so `A1012` is preferred over `A101` when both are listed.
    for key in sorted(by_normalised, key=len, reverse=True):
        if len(key) >= 4 and key in stem:
            return by_normalised[key]
    return None


def _match_by_title(sheet: SheetRecord, titles: dict[str, ListEntry]) -> ListEntry | None:
    """A file name that reads like a title on the list."""
    if not titles:
        return None

    candidate = _title_key(sheet.filename.rsplit(".", 1)[0])
    if len(candidate) < 6:
        return None  # too short to be a title; too easy to match by accident

    match = process.extractOne(
        candidate, list(titles), scorer=fuzz.token_sort_ratio, score_cutoff=TITLE_MATCH_THRESHOLD
    )
    if match is None:
        return None
    return titles[match[0]]


def _adopt(sheet: SheetRecord, entry: ListEntry, why: str) -> None:
    """Take a number from the list, recording that it did not come from the sheet."""
    sheet.drawing_no = entry.drawing_no
    sheet.normalised_no = entry.normalised_no or normalise_number(entry.drawing_no)
    sheet.source_of_number = str(NumberSource.DRAWING_LIST)
    sheet.number_confidence = LIST_CONFIDENCE
    if entry.title and not sheet.title:
        sheet.title = entry.title
    if entry.revision and not sheet.revision:
        sheet.revision = entry.revision

    sheet.warnings = [
        f"This number came from the drawing list, not from the sheet, because {why}. "
        "Open the drawing and confirm it."
    ]
