"""Load a drawing list from whatever the design team sent.

Excel is the usual case. CSV happens. PDF happens more often than it should,
and a PDF table has no cells at all — only text with coordinates — so the
columns are recovered by clustering the x positions.

Every path ends in the same place: a grid of cells handed to
:mod:`engine.register.list_parser`, which proposes a mapping for the user to
confirm. When confidence is low this says so, rather than returning data that
looks fine and is wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from engine.register.list_parser import (
    ParseResult,
    parse_grid,
    score_cells_as_drawing_numbers,
)
from engine.utils.errors import UnreadableFileError, ValidationError
from engine.utils.longpath import long_path

SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".xlsx", ".xlsm", ".xls", ".csv", ".pdf"})

#: How many rows of a worksheet to read when scoring it.
SCORE_ROWS = 60
#: Columns in a PDF table are found by clustering x positions this far apart.
PDF_COLUMN_TOLERANCE = 12.0


def import_drawing_list(path: str | Path) -> ParseResult:
    """Read a drawing list and propose how to interpret it."""
    target = Path(str(path))
    suffix = target.suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        raise ValidationError(
            f"{suffix or 'This file type'} cannot be read as a drawing list. "
            "Use an Excel file, a CSV, or a PDF.",
            detail={"path": str(target), "supported": sorted(SUPPORTED_SUFFIXES)},
        )

    if not target.is_file():
        raise UnreadableFileError(
            "That drawing list could not be found. It may have been moved, or the "
            "network drive may be disconnected.",
            detail={"path": str(target)},
        )

    if suffix == ".csv":
        result = _from_csv(target)
    elif suffix == ".pdf":
        result = _from_pdf(target)
    else:
        result = _from_excel(target)

    result.source_path = str(target)
    logger.info(
        "Drawing list {} | sheet={} | header row={} | {} rows | confidence {:.0%}",
        target.name,
        result.sheet_name,
        result.header_row,
        result.row_count,
        result.confidence,
    )
    return result


# ── Excel ──────────────────────────────────────────────────────────────


def read_workbook_grids(path: Path) -> dict[str, list[list[Any]]]:
    """Every worksheet as a grid of cell values."""
    import pandas as pd

    try:
        book = pd.read_excel(long_path(path), sheet_name=None, header=None, dtype=object)
    except Exception as exc:
        raise UnreadableFileError(
            "This spreadsheet could not be opened. It may be damaged, or saved in "
            "a format the application cannot read. Try re-saving it as .xlsx.",
            detail={"path": str(path), "error": str(exc)},
        ) from exc

    return {name: frame.values.tolist() for name, frame in book.items()}


def _from_excel(path: Path) -> ParseResult:
    """Pick the worksheet that holds the register, then parse it.

    A workbook usually has several sheets and only one of them is the list, so
    each is scored by how many cells look like a drawing number.
    """
    grids = read_workbook_grids(path)
    if not grids:
        raise UnreadableFileError("This spreadsheet has no worksheets.", detail={"path": str(path)})

    scores: dict[str, int] = {}
    for name, grid in grids.items():
        cells = [cell for row in grid[:SCORE_ROWS] for cell in row]
        scores[name] = score_cells_as_drawing_numbers(cells)

    best_name = max(scores.items(), key=lambda pair: pair[1])[0]
    logger.debug("Worksheet scores: {} -> chose {!r}", scores, best_name)

    result = parse_grid(grids[best_name], sheet_name=best_name)
    result.sheet_names = list(grids)

    if scores[best_name] == 0:
        result.warnings.insert(
            0,
            "No cells in this workbook look like drawing numbers. Check this is "
            "the right file, or choose a different worksheet.",
        )
        result.confidence = 0.0
    elif len(grids) > 1:
        result.warnings.append(
            f"This workbook has {len(grids)} worksheets. '{best_name}' looked most "
            "like the register. Choose a different one if that is wrong."
        )

    return result


def parse_named_sheet(path: str | Path, sheet_name: str) -> ParseResult:
    """Re-read a workbook using a worksheet the user picked."""
    grids = read_workbook_grids(Path(str(path)))
    if sheet_name not in grids:
        raise ValidationError(
            f"There is no worksheet named {sheet_name!r} in this workbook.",
            detail={"available": list(grids)},
        )

    result = parse_grid(grids[sheet_name], sheet_name=sheet_name)
    result.sheet_names = list(grids)
    result.source_path = str(path)
    return result


# ── CSV ────────────────────────────────────────────────────────────────


def _from_csv(path: Path) -> ParseResult:
    import csv

    rows: list[list[Any]] = []
    try:
        with open(long_path(path), encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel  # a single-column file is still readable
            rows = [list(row) for row in csv.reader(handle, dialect)]
    except OSError as exc:
        raise UnreadableFileError(
            "This CSV file could not be read.", detail={"path": str(path), "error": str(exc)}
        ) from exc

    result = parse_grid(rows, sheet_name=path.name)
    result.sheet_names = [path.name]
    return result


# ── PDF ────────────────────────────────────────────────────────────────


def _from_pdf(path: Path) -> ParseResult:
    """Recover a table from a PDF by clustering text into columns.

    There is no cell structure to read, so this is a reconstruction. It is
    reported with lower confidence, and says so plainly rather than pretending
    otherwise.
    """
    from engine.extract.text_extractor import extract_document_text

    pages = extract_document_text(str(path))
    if not pages:
        raise UnreadableFileError(
            "No text could be read from this PDF. If it is a scan, the drawing "
            "list cannot be imported from it.",
            detail={"path": str(path)},
        )

    best: ParseResult | None = None
    best_score = -1

    for index, page in sorted(pages.items()):
        grid = _page_to_grid(page)
        if not grid:
            continue
        score = score_cells_as_drawing_numbers([cell for row in grid for cell in row])
        if score > best_score:
            parsed = parse_grid(grid, sheet_name=f"Page {index + 1}")
            best, best_score = parsed, score

    if best is None:
        raise UnreadableFileError(
            "No table could be found in this PDF. Ask for the drawing list as an "
            "Excel file instead.",
            detail={"path": str(path)},
        )

    best.sheet_names = [f"Page {index + 1}" for index in sorted(pages)]
    # A reconstructed table is never as trustworthy as real cells.
    best.confidence = min(best.confidence, 0.5)
    best.warnings.insert(
        0,
        "This list was read from a PDF, where there are no real table cells. "
        "Check the preview carefully before continuing.",
    )
    return best


def _page_to_grid(page: Any) -> list[list[str]]:
    """Cluster a page's text into rows and columns by position."""
    if page.is_empty:
        return []

    # Rows: text sharing a baseline.
    rows: dict[int, list[Any]] = {}
    for item in page.items:
        if not item.clean:
            continue
        key = round(item.y / max(item.height, 1.0))
        rows.setdefault(key, []).append(item)

    if not rows:
        return []

    # Columns: cluster the left edges used across the whole page.
    lefts = sorted({round(item.x, 1) for line in rows.values() for item in line})
    columns: list[float] = []
    for left in lefts:
        if not columns or left - columns[-1] > PDF_COLUMN_TOLERANCE:
            columns.append(left)

    grid: list[list[str]] = []
    for key in sorted(rows, reverse=True):  # top of the page downwards
        cells = [""] * len(columns)
        for item in sorted(rows[key], key=lambda entry: entry.x):
            index = min(
                range(len(columns)),
                key=lambda position: abs(columns[position] - item.x),
            )
            cells[index] = f"{cells[index]} {item.clean}".strip()
        grid.append(cells)

    return grid
