"""Read a drawing register that a human formatted for printing.

Real drawing lists are not data files. They have a logo and a project block
above the header row, merged cells, blank separator rows, discipline headings
inside the data, and column names that vary by office. Some do not have a
revision column at all: they have a *revision matrix*, one column per issue
date with the revision letters inside the grid.

The approach is deliberately not fully automatic. The parser proposes a
worksheet, a header row and a column mapping, and returns a preview of the
first rows so the user can correct it. **Manual mapping with a good preview
beats clever automatic parsing that gets it wrong silently**, because a
mis-parsed list produces a register full of drawings that do not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from engine.titleblock.patterns import find_numbers, is_plausible_number

#: Fields the register needs from a drawing list.
FIELDS: tuple[str, ...] = ("drawing_no", "title", "revision", "status", "date")

#: Column heading synonyms, per field, lower case.
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "drawing_no": (
        "drawing no",
        "drawing number",
        "drawing ref",
        "dwg no",
        "dwg no.",
        "dwg number",
        "drg no",
        "document no",
        "document number",
        "doc no",
        "sheet no",
        "sheet number",
        "sheet ref",
        "number",
        "no",
        "ref",
    ),
    "title": ("title", "drawing title", "sheet name", "sheet title", "description", "name"),
    "revision": ("rev", "rev.", "revision", "rev no", "issue", "current rev", "latest rev"),
    "status": ("status", "purpose", "suitability", "issue status"),
    "date": ("date", "issue date", "revision date", "dated"),
}

#: Words that mark a row as a header row rather than data.
HEADER_WORDS: frozenset[str] = frozenset(
    {"drawing", "dwg", "drg", "no", "number", "title", "rev", "sheet", "description", "status"}
)

#: How many rows to look through for the header before giving up.
HEADER_SEARCH_ROWS = 25
#: How many parsed rows to show the user for confirmation.
PREVIEW_ROWS = 15

#: A date-like column heading, which is what a revision matrix looks like.
_DATE_HEADING = re.compile(
    r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.IGNORECASE,
)

#: Discipline headings that sit inside the data.
_SECTION_WORDS = frozenset(
    {
        "architectural",
        "structural",
        "civil",
        "mechanical",
        "electrical",
        "plumbing",
        "mep",
        "landscape",
        "interior",
        "general",
        "drainage",
        "fire",
    }
)


@dataclass(slots=True)
class ParsedRow:
    """One data row, as understood."""

    row_number: int
    drawing_no: str
    title: str | None = None
    revision: str | None = None
    status: str | None = None
    date: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "drawing_no": self.drawing_no,
            "title": self.title,
            "revision": self.revision,
            "status": self.status,
            "date": self.date,
        }


@dataclass(slots=True)
class ParseResult:
    """A proposal for how to read the list, for the user to confirm."""

    source_path: str = ""
    sheet_name: str | None = None
    sheet_names: list[str] = field(default_factory=list)
    header_row: int | None = None
    #: field name -> column heading, as proposed.
    mapping: dict[str, str] = field(default_factory=dict)
    #: Every column heading found, so the user can pick a different one.
    columns: list[str] = field(default_factory=list)
    preview: list[ParsedRow] = field(default_factory=list)
    rows: list[ParsedRow] = field(default_factory=list)
    is_revision_matrix: bool = False
    revision_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.rows) and "drawing_no" in self.mapping

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "sheet_name": self.sheet_name,
            "sheet_names": list(self.sheet_names),
            "header_row": self.header_row,
            "mapping": dict(self.mapping),
            "columns": list(self.columns),
            "preview": [row.as_dict() for row in self.preview],
            "row_count": self.row_count,
            "is_revision_matrix": self.is_revision_matrix,
            "revision_columns": list(self.revision_columns),
            "warnings": list(self.warnings),
            "confidence": round(self.confidence, 3),
            "ok": self.ok,
        }


# ── Scoring and detection ──────────────────────────────────────────────


def score_cells_as_drawing_numbers(cells: list[Any]) -> int:
    """How many cells in this range look like a drawing number."""
    count = 0
    for cell in cells:
        text = _text(cell)
        if text and is_plausible_number(text) and find_numbers(text):
            count += 1
    return count


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def find_header_row(rows: list[list[Any]]) -> int | None:
    """The row that names the columns.

    Scanned by counting header words, because the header is rarely row 1: a
    logo and a project information block usually sit above it.
    """
    best_index: int | None = None
    best_score = 1  # a header row needs at least two recognisable words

    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        words: set[str] = set()
        for cell in row:
            text = _text(cell).lower()
            if not text or len(text) > 40:
                continue
            for word in HEADER_WORDS:
                if word in text:
                    words.add(word)
        if len(words) > best_score:
            best_index, best_score = index, len(words)

    return best_index


def guess_mapping(headings: list[str]) -> dict[str, str]:
    """Match column headings to the fields the register needs."""
    mapping: dict[str, str] = {}
    used: set[str] = set()

    normalised = [(_text(heading), _text(heading).lower().strip(" .:_-")) for heading in headings]

    for field_name in FIELDS:
        synonyms = COLUMN_SYNONYMS[field_name]
        # Exact matches first, then "contains", so "Drawing No." wins over "No".
        for exact in (True, False):
            for original, lowered in normalised:
                if not original or original in used:
                    continue
                hit = (
                    lowered in synonyms
                    if exact
                    else any(word in lowered for word in synonyms if len(word) > 3)
                )
                if hit:
                    mapping[field_name] = original
                    used.add(original)
                    break
            if field_name in mapping:
                break

    return mapping


def detect_revision_matrix(headings: list[str]) -> list[str]:
    """Date-like columns, which is what a revision matrix looks like."""
    return [heading for heading in headings if _DATE_HEADING.search(_text(heading))]


def is_section_heading(values: dict[str, str]) -> bool:
    """A discipline heading sitting inside the data, e.g. "ARCHITECTURAL"."""
    filled = [text for text in values.values() if text]
    if len(filled) != 1:
        return False
    return filled[0].strip().lower() in _SECTION_WORDS


# ── Parsing ────────────────────────────────────────────────────────────


def parse_rows(
    rows: list[list[Any]],
    header_index: int,
    mapping: dict[str, str],
    headings: list[str],
    revision_columns: list[str] | None = None,
) -> tuple[list[ParsedRow], list[str]]:
    """Turn the rows below the header into data, skipping the noise."""
    warnings: list[str] = []
    parsed: list[ParsedRow] = []
    position = {heading: index for index, heading in enumerate(headings)}

    number_column = mapping.get("drawing_no")
    if number_column is None:
        return [], ["No drawing number column could be identified."]

    skipped_blank = 0
    skipped_sections = 0

    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        values = {
            heading: _text(row[index]) if index < len(row) else ""
            for heading, index in position.items()
        }

        if not any(values.values()):
            skipped_blank += 1
            continue
        if is_section_heading(values):
            skipped_sections += 1
            continue

        number = values.get(number_column, "")
        if not number:
            skipped_blank += 1
            continue
        if not is_plausible_number(number):
            continue

        revision = values.get(mapping.get("revision", ""), "") or None
        if revision_columns:
            # A revision matrix keeps the codes in the grid. The most recent
            # issue is the right-most column with something in it.
            for heading in reversed(revision_columns):
                if values.get(heading):
                    revision = values[heading]
                    break

        parsed.append(
            ParsedRow(
                row_number=offset,
                drawing_no=number.strip().upper(),
                title=values.get(mapping.get("title", ""), "") or None,
                revision=revision,
                status=values.get(mapping.get("status", ""), "") or None,
                date=values.get(mapping.get("date", ""), "") or None,
            )
        )

    if skipped_sections:
        warnings.append(f"Skipped {skipped_sections} discipline heading rows.")
    if skipped_blank:
        warnings.append(f"Skipped {skipped_blank} blank or incomplete rows.")

    return parsed, warnings


def parse_grid(grid: list[list[Any]], sheet_name: str | None = None) -> ParseResult:
    """Parse one worksheet's worth of cells."""
    result = ParseResult(sheet_name=sheet_name)

    if not grid:
        result.warnings.append("This sheet is empty.")
        return result

    header_index = find_header_row(grid)
    if header_index is None:
        result.warnings.append(
            "No header row could be found in the first 25 rows. Check this is the "
            "right worksheet, or set the columns by hand."
        )
        return result

    headings = [_text(cell) for cell in grid[header_index]]
    result.header_row = header_index + 1  # 1-based, as the user sees it
    result.columns = [heading for heading in headings if heading]
    result.mapping = guess_mapping(headings)

    result.revision_columns = detect_revision_matrix(headings)
    result.is_revision_matrix = (
        len(result.revision_columns) >= 2 and "revision" not in result.mapping
    )
    if result.is_revision_matrix:
        result.warnings.append(
            f"This list uses a revision matrix with {len(result.revision_columns)} issue "
            "columns. The most recent one with an entry has been used for each row."
        )

    if "drawing_no" not in result.mapping:
        result.warnings.append(
            "No drawing number column was recognised. Choose it from the list of columns."
        )
        return result

    rows, warnings = parse_rows(
        grid,
        header_index,
        result.mapping,
        headings,
        result.revision_columns if result.is_revision_matrix else None,
    )
    result.rows = rows
    result.preview = rows[:PREVIEW_ROWS]
    result.warnings.extend(warnings)

    if not rows:
        result.warnings.append("No drawing rows were found under the header.")
        result.confidence = 0.0
    else:
        # Confidence comes from how much of what we need was actually found.
        found_fields = sum(
            1 for name in ("drawing_no", "title", "revision") if name in result.mapping
        )
        result.confidence = min(1.0, 0.4 + 0.2 * found_fields)

    return result


def normalise_entries(result: ParseResult) -> list[Any]:
    """Convert parsed rows into the register's `ListEntry` model."""
    from engine.core.models import ListEntry
    from engine.titleblock.field_extractor import normalise_number

    return [
        ListEntry(
            drawing_no=row.drawing_no,
            normalised_no=normalise_number(row.drawing_no),
            title=row.title,
            revision=row.revision,
            row_number=row.row_number,
        )
        for row in result.rows
    ]


def apply_mapping(
    result: ParseResult, grid: list[list[Any]], mapping: dict[str, str]
) -> ParseResult:
    """Re-parse with a mapping the user corrected."""
    if result.header_row is None:
        return result

    header_index = result.header_row - 1
    headings = [_text(cell) for cell in grid[header_index]]

    updated = ParseResult(
        source_path=result.source_path,
        sheet_name=result.sheet_name,
        sheet_names=list(result.sheet_names),
        header_row=result.header_row,
        mapping=dict(mapping),
        columns=list(result.columns),
        is_revision_matrix=result.is_revision_matrix,
        revision_columns=list(result.revision_columns),
    )
    rows, warnings = parse_rows(
        grid,
        header_index,
        mapping,
        headings,
        updated.revision_columns if updated.is_revision_matrix else None,
    )
    updated.rows = rows
    updated.preview = rows[:PREVIEW_ROWS]
    updated.warnings = warnings
    updated.confidence = 1.0 if rows else 0.0

    logger.info("Drawing list re-parsed with the corrected mapping: {} rows", len(rows))
    return updated
