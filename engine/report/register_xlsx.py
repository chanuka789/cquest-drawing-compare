"""The drawing register as an Excel workbook.

This is the thing a document controller actually sends to the design team, so
it is written to be read by a person, not to be re-imported by a machine.

Four sheets:

1. **Summary** — what was compared, with what settings, and the counts.
   Written so it can be pasted straight into an email.
2. **Register** — every drawing, with a frozen header, an autofilter, and a
   "How the number was found" column so the reader can judge each row.
3. **Needs attention** — only the flagged rows, each with a plain-English
   explanation of what is wrong and what to do.
4. **Not readable** — the quarantine list, with reasons.

Note on colour: brand red is correct *here*. The rule that keeps it out of the
diff palette applies to the application chrome and the drawing canvas. This is
a document, and the red is the letterhead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xlsxwriter
from loguru import logger

from engine.core.enums import IssueType, RegisterStatus
from engine.core.models import RegisterRow, RegisterSummary
from engine.core.workspace import Workspace, unique_path

BRAND_RED = "#CF0A2C"
WHITE = "#FFFFFF"

#: Status colours, matching the pills in the UI.
STATUS_FILL: dict[RegisterStatus, str] = {
    RegisterStatus.REVISED: "#2E9BD6",
    RegisterStatus.STATUS_CHANGE: "#2E9BD6",
    RegisterStatus.NEW: "#2BB673",
    RegisterStatus.UNCHANGED: "#F2F2F2",
    RegisterStatus.NOT_REISSUED: "#F2F2F2",
    RegisterStatus.SAME_REV_DIFFERENT_FILE: "#E39A00",
    RegisterStatus.REMOVED: "#E8442A",
    RegisterStatus.UNIDENTIFIED: "#E39A00",
    RegisterStatus.UNREADABLE: "#E8442A",
    RegisterStatus.IN_LIST_NOT_IN_FOLDER: "#E39A00",
    RegisterStatus.IN_FOLDER_NOT_IN_LIST: "#E39A00",
    RegisterStatus.SUPERSEDED_IN_FOLDER: "#F2F2F2",
    RegisterStatus.DUPLICATE_FILE: "#F2F2F2",
}

#: How each status reads in the export. Sentence case, no jargon.
STATUS_LABEL: dict[RegisterStatus, str] = {
    RegisterStatus.REVISED: "Revised",
    RegisterStatus.UNCHANGED: "Unchanged",
    RegisterStatus.SAME_REV_DIFFERENT_FILE: "Same revision, different file",
    RegisterStatus.NEW: "New",
    RegisterStatus.NOT_REISSUED: "Not reissued",
    RegisterStatus.REMOVED: "Removed",
    RegisterStatus.STATUS_CHANGE: "Status change",
    RegisterStatus.SUPERSEDED_IN_FOLDER: "Superseded in folder",
    RegisterStatus.DUPLICATE_FILE: "Duplicate file",
    RegisterStatus.UNIDENTIFIED: "Unidentified",
    RegisterStatus.UNREADABLE: "Could not be read",
    RegisterStatus.IN_LIST_NOT_IN_FOLDER: "On the list, not in the folders",
    RegisterStatus.IN_FOLDER_NOT_IN_LIST: "In the folder, not on the list",
}

#: How each `source_of_number` reads in the export.
SOURCE_LABEL: dict[str, str] = {
    "titleblock": "Title block",
    "sheet_text": "Sheet text",
    "filename": "File name",
    "drawing_list": "Drawing list",
    "user": "Entered by user",
    "ai": "Read by AI",
    "none": "Not found",
}

REGISTER_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Drawing no.", 26),
    ("Title", 44),
    ("Previous rev", 13),
    ("Current rev", 13),
    ("Status", 26),
    ("How the number was found", 22),
    ("On the drawing list", 17),
    ("Note", 70),
)


def _formats(book: xlsxwriter.Workbook) -> dict[str, Any]:
    """Every cell format used, defined once."""
    return {
        "title": book.add_format({"bold": True, "font_size": 16, "font_color": BRAND_RED}),
        "heading": book.add_format(
            {
                "bold": True,
                "font_color": WHITE,
                "bg_color": BRAND_RED,
                "border": 1,
                "align": "left",
                "valign": "vcenter",
                "text_wrap": True,
            }
        ),
        "label": book.add_format({"bold": True, "align": "left", "valign": "top"}),
        "value": book.add_format({"align": "left", "valign": "top", "text_wrap": True}),
        "wrap": book.add_format({"valign": "top", "text_wrap": True}),
        "number": book.add_format({"align": "left", "valign": "top", "num_format": "@"}),
        "count": book.add_format({"bold": True, "align": "right"}),
        "path": book.add_format({"valign": "top", "font_size": 9, "font_color": "#666666"}),
    }


def _status_formats(book: xlsxwriter.Workbook) -> dict[str, Any]:
    formats: dict[str, Any] = {}
    for status, colour in STATUS_FILL.items():
        dark = colour in {"#F2F2F2"}
        formats[status] = book.add_format(
            {
                "bg_color": colour,
                "font_color": "#1B2127" if dark else WHITE,
                "align": "left",
                "valign": "top",
                "border": 1,
                "border_color": "#DDDDDD",
            }
        )
    return formats


def _write_summary(
    book: xlsxwriter.Workbook,
    formats: dict[str, Any],
    summary: RegisterSummary,
    context: dict[str, Any],
) -> None:
    """Sheet 1: what was compared, and the headline numbers."""
    sheet = book.add_worksheet("Summary")
    sheet.set_column("A:A", 26)
    sheet.set_column("B:B", 90)
    sheet.hide_gridlines(2)

    sheet.write(0, 0, "Drawing register", formats["title"])
    row = 2

    facts: list[tuple[str, Any]] = [
        ("Project", context.get("project_name", "")),
        ("Previous issue", context.get("old_folder", "")),
        ("Current issue", context.get("new_folder", "")),
        ("Drawing list", context.get("drawing_list", "") or "Not used"),
        ("Issue type", _issue_type_label(summary.issue_type)),
        ("Sheet profile", context.get("profile", "default")),
        ("Drawings in previous issue", summary.old_sheet_count),
        ("Drawings in current issue", summary.new_sheet_count),
        ("Run on", context.get("run_at", datetime.now(UTC).strftime("%d %B %Y, %H:%M"))),
        ("Application version", context.get("version", "")),
    ]
    for label, value in facts:
        sheet.write(row, 0, label, formats["label"])
        sheet.write(row, 1, value, formats["value"])
        row += 1

    row += 1
    sheet.write(row, 0, "In one sentence", formats["label"])
    sheet.write(row, 1, summary.sentence, formats["wrap"])
    row += 2

    sheet.write(row, 0, "Status", formats["heading"])
    sheet.write(row, 1, "Drawings", formats["heading"])
    row += 1

    from engine.register.summary import SENTENCE_ORDER

    for status in SENTENCE_ORDER:
        count = summary.counts.get(str(status), 0)
        if not count:
            continue
        sheet.write(row, 0, STATUS_LABEL.get(status, str(status)), formats["value"])
        sheet.write_number(row, 1, count, formats["count"])
        row += 1

    if summary.attention_count:
        row += 1
        sheet.write(row, 0, "Needs attention", formats["label"])
        sheet.write(
            row,
            1,
            f"{summary.attention_count} drawings are listed on the "
            "'Needs attention' sheet of this workbook.",
            formats["wrap"],
        )


def _issue_type_label(issue_type: IssueType) -> str:
    return {
        IssueType.PARTIAL: "Partial issue - only the changed drawings were reissued",
        IssueType.FULL: "Full issue - the complete set",
        IssueType.UNKNOWN: "Not confirmed",
    }[issue_type]


def _write_register(
    book: xlsxwriter.Workbook,
    formats: dict[str, Any],
    status_formats: dict[str, Any],
    rows: Sequence[RegisterRow],
    name: str = "Register",
) -> None:
    """Sheet 2: the whole table, filterable."""
    sheet = book.add_worksheet(name)

    for index, (heading, width) in enumerate(REGISTER_COLUMNS):
        sheet.set_column(index, index, width)
        sheet.write(0, index, heading, formats["heading"])

    sheet.freeze_panes(1, 0)
    sheet.set_row(0, 30)
    if rows:
        sheet.autofilter(0, 0, len(rows), len(REGISTER_COLUMNS) - 1)

    for offset, row in enumerate(rows, start=1):
        status = RegisterStatus(row.status)
        sheet.write_string(offset, 0, row.drawing_no, formats["number"])
        sheet.write_string(offset, 1, row.title or "", formats["wrap"])
        sheet.write_string(offset, 2, row.old_revision or "", formats["number"])
        sheet.write_string(offset, 3, row.new_revision or "", formats["number"])
        sheet.write_string(
            offset,
            4,
            STATUS_LABEL.get(status, str(status)),
            status_formats.get(status, formats["value"]),
        )
        sheet.write_string(
            offset,
            5,
            SOURCE_LABEL.get(row.source_of_number, row.source_of_number),
            formats["value"],
        )
        sheet.write_string(offset, 6, _list_label(row.in_drawing_list), formats["value"])
        sheet.write_string(offset, 7, row.note, formats["wrap"])


def _list_label(value: bool | None) -> str:
    if value is None:
        return "No list imported"
    return "Yes" if value else "No"


def _write_attention(
    book: xlsxwriter.Workbook,
    formats: dict[str, Any],
    status_formats: dict[str, Any],
    rows: Sequence[RegisterRow],
) -> None:
    """Sheet 3: only the rows someone has to look at."""
    flagged = [row for row in rows if row.needs_attention]
    sheet = book.add_worksheet("Needs attention")

    sheet.set_column("A:A", 26)
    sheet.set_column("B:B", 40)
    sheet.set_column("C:C", 26)
    sheet.set_column("D:D", 90)
    sheet.set_row(0, 30)

    for index, heading in enumerate(("Drawing no.", "Title", "Status", "What to do")):
        sheet.write(0, index, heading, formats["heading"])
    sheet.freeze_panes(1, 0)

    if not flagged:
        sheet.write(1, 0, "Nothing needs attention.", formats["value"])
        return

    for offset, row in enumerate(flagged, start=1):
        status = RegisterStatus(row.status)
        sheet.write_string(offset, 0, row.drawing_no, formats["number"])
        sheet.write_string(offset, 1, row.title or "", formats["wrap"])
        sheet.write_string(
            offset,
            2,
            STATUS_LABEL.get(status, str(status)),
            status_formats.get(status, formats["value"]),
        )
        note = row.note
        if row.number_mismatch:
            note = (
                f"{note} The title block and the file name give different drawing "
                "numbers; check which is correct."
            ).strip()
        sheet.write_string(offset, 3, note, formats["wrap"])


def _write_quarantine(
    book: xlsxwriter.Workbook, formats: dict[str, Any], entries: Sequence[dict[str, str]]
) -> None:
    """Sheet 4: files that could not be opened, and why."""
    sheet = book.add_worksheet("Not readable")

    sheet.set_column("A:A", 34)
    sheet.set_column("B:B", 22)
    sheet.set_column("C:C", 70)
    sheet.set_column("D:D", 70)
    sheet.set_row(0, 30)

    for index, heading in enumerate(("File", "Reason", "What to do", "Full path")):
        sheet.write(0, index, heading, formats["heading"])
    sheet.freeze_panes(1, 0)

    if not entries:
        sheet.write(1, 0, "Every file was read successfully.", formats["value"])
        return

    for offset, entry in enumerate(entries, start=1):
        sheet.write_string(offset, 0, entry.get("filename", ""), formats["value"])
        sheet.write_string(offset, 1, entry.get("label", ""), formats["value"])
        sheet.write_string(offset, 2, entry.get("advice", ""), formats["wrap"])
        sheet.write_string(offset, 3, entry.get("path", ""), formats["path"])


def write_register(
    path: str | Path,
    rows: Sequence[RegisterRow],
    summary: RegisterSummary,
    *,
    quarantine: Sequence[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
) -> Path:
    """Write the register workbook. Never overwrites an existing file."""
    target = unique_path(Path(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)

    book = xlsxwriter.Workbook(str(target), {"constant_memory": False})
    try:
        formats = _formats(book)
        status_formats = _status_formats(book)

        _write_summary(book, formats, summary, context or {})
        _write_register(book, formats, status_formats, rows)
        _write_attention(book, formats, status_formats, rows)
        _write_quarantine(book, formats, quarantine or [])
    finally:
        book.close()

    logger.info("Register written to {} | {} rows", target, len(rows))
    return target


def write_register_to_workspace(
    workspace: Workspace,
    rows: Sequence[RegisterRow],
    summary: RegisterSummary,
    *,
    quarantine: Sequence[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
    filename: str = "Drawing_Register.xlsx",
) -> Path:
    """Write the register into `01_Register` inside the workspace."""
    return write_register(
        workspace.register_dir / filename,
        rows,
        summary,
        quarantine=quarantine,
        context=context,
    )
