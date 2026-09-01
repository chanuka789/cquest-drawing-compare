"""Set reconciliation: what is new, missing, revised and unchanged.

This is the core of Phase 2 and the thing that decides whether anyone trusts
the tool. Three rules matter more than the rest.

**A partial issue is normal.** Consultants reissue the twelve sheets that
changed, not all three hundred. If the app reports "288 drawings removed" on a
partial issue, the user closes it and never opens it again — the tool looks
broken even though the code is right. So when the sets are very different
sizes and nobody has said which kind of issue this is, the register is not
produced: a question is returned instead.

**Same revision, different file.** The revision is identical but the content
hash differs, which means someone reissued a drawing without bumping the
revision. It is easy to miss by hand, it is exactly what a document controller
is paid to catch, and it is flagged loudly.

**Never say "removed" until the issue type is known.** On a partial issue an
old-only drawing is `not_reissued`, which needs no action at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from loguru import logger

from engine.core.enums import NEEDS_ATTENTION, IssueType, RegisterStatus, Side
from engine.core.models import (
    IssueTypeQuestion,
    ListEntry,
    ReconcileResult,
    RegisterRow,
    SheetRecord,
)
from engine.register.revision_logic import (
    RevisionComparison,
    compare_revisions,
    describe_change,
    parse_revision,
)
from engine.titleblock.field_extractor import normalise_number

#: Below this ratio of new sheets to old, the issue is probably partial and
#: the user is asked rather than guessed at.
PARTIAL_ISSUE_RATIO = 0.6


@dataclass(slots=True)
class _Group:
    """Every sheet found for one drawing number on one side."""

    best: SheetRecord
    superseded: list[SheetRecord] = field(default_factory=list)
    duplicates: list[SheetRecord] = field(default_factory=list)


def _pick_latest(sheets: list[SheetRecord]) -> _Group:
    """Keep the highest revision; log the rest as superseded or duplicate.

    Within one folder a drawing number can appear at two revisions, usually
    because an older file was never cleared out. The newest is the real one.
    """
    if len(sheets) == 1:
        return _Group(best=sheets[0])

    by_hash: dict[str, list[SheetRecord]] = {}
    for sheet in sheets:
        by_hash.setdefault(sheet.content_hash or sheet.abs_path, []).append(sheet)

    # Identical content in two places is a duplicate, not a supersession.
    duplicates: list[SheetRecord] = []
    unique: list[SheetRecord] = []
    for group in by_hash.values():
        unique.append(group[0])
        duplicates.extend(group[1:])

    def sort_key(sheet: SheetRecord) -> tuple[int, str]:
        parsed = parse_revision(sheet.revision)
        return (parsed.ordinal if parsed.ordinal is not None else -1, sheet.abs_path)

    unique.sort(key=sort_key, reverse=True)
    return _Group(best=unique[0], superseded=unique[1:], duplicates=duplicates)


def _index_by_number(sheets: Iterable[SheetRecord]) -> tuple[dict[str, _Group], list[SheetRecord]]:
    """Group identified sheets by normalised number; return the rest separately."""
    buckets: dict[str, list[SheetRecord]] = {}
    unidentified: list[SheetRecord] = []

    for sheet in sheets:
        if not sheet.identified:
            unidentified.append(sheet)
            continue
        key = sheet.normalised_no or normalise_number(sheet.drawing_no)
        buckets.setdefault(key, []).append(sheet)

    return {key: _pick_latest(group) for key, group in buckets.items()}, unidentified


def _looks_partial(old_count: int, new_count: int) -> bool:
    if old_count == 0:
        return False
    return new_count < old_count * PARTIAL_ISSUE_RATIO


def _issue_type_question(old_count: int, new_count: int) -> IssueTypeQuestion:
    """Ask, do not assume. The answer changes the whole register."""
    return IssueTypeQuestion(
        old_count=old_count,
        new_count=new_count,
        question=(
            f"The current issue has {new_count} drawings and the previous issue "
            f"has {old_count}. Is this a partial issue, where only the changed "
            "drawings were reissued, or a full issue containing the complete set?"
        ),
        options=[
            {
                "id": IssueType.PARTIAL,
                "label": "Partial issue",
                "description": (
                    f"Only the changed drawings were reissued. The other "
                    f"{max(0, old_count - new_count)} are simply not in this issue."
                ),
            },
            {
                "id": IssueType.FULL,
                "label": "Full issue",
                "description": (
                    "This is the complete set. Anything missing from it may have "
                    "been deleted from the scope."
                ),
            },
            {
                "id": "compare_reissued",
                "label": "Compare only what was reissued",
                "description": "Treat it as a partial issue and ignore the rest.",
            },
        ],
    )


def reconcile(
    old_sheets: Sequence[SheetRecord],
    new_sheets: Sequence[SheetRecord],
    *,
    drawing_list: Sequence[ListEntry] | None = None,
    issue_type: IssueType = IssueType.UNKNOWN,
) -> ReconcileResult:
    """Build the drawing register from both issues and an optional list."""
    old_index, old_unidentified = _index_by_number(old_sheets)
    new_index, new_unidentified = _index_by_number(new_sheets)

    # The question comes before the answer: a wrong issue type makes every
    # old-only row say the wrong thing.
    if issue_type is IssueType.UNKNOWN and _looks_partial(len(old_sheets), len(new_sheets)):
        logger.info(
            "Set sizes differ sharply ({} old, {} new); asking the user for the issue type",
            len(old_sheets),
            len(new_sheets),
        )
        return ReconcileResult(
            needs_issue_type_confirmation=True,
            issue_type_question=_issue_type_question(len(old_sheets), len(new_sheets)),
        )

    list_index: dict[str, ListEntry] = {}
    if drawing_list:
        for entry in drawing_list:
            key = entry.normalised_no or normalise_number(entry.drawing_no)
            list_index.setdefault(key, entry)

    rows: list[RegisterRow] = []
    for key in sorted(set(old_index) | set(new_index)):
        rows.append(
            _build_row(
                key,
                old_index.get(key),
                new_index.get(key),
                list_index,
                issue_type,
                bool(drawing_list),
            )
        )

    rows.extend(_unidentified_rows(old_unidentified, Side.OLD))
    rows.extend(_unidentified_rows(new_unidentified, Side.NEW))

    if drawing_list:
        rows.extend(_missing_from_folders(list_index, old_index, new_index))

    for row in rows:
        row.needs_attention = row.status in NEEDS_ATTENTION or row.number_mismatch

    rows.sort(key=_sort_key)

    from engine.register.summary import build_summary

    summary = build_summary(rows, len(old_sheets), len(new_sheets), issue_type)
    return ReconcileResult(rows=rows, summary=summary)


def _sort_key(row: RegisterRow) -> tuple[int, str]:
    """Rows needing attention come first; then by drawing number."""
    return (0 if row.needs_attention else 1, row.drawing_no)


def _build_row(
    key: str,
    old: _Group | None,
    new: _Group | None,
    list_index: dict[str, ListEntry],
    issue_type: IssueType,
    have_list: bool,
) -> RegisterRow:
    """Decide the status of one drawing number."""
    in_list = key in list_index if have_list else None
    entry = list_index.get(key)

    if old is not None and new is not None:
        return _build_matched_row(key, old, new, in_list)

    if new is not None:
        sheet = new.best
        row = RegisterRow(
            drawing_no=sheet.drawing_no or key,
            status=RegisterStatus.NEW,
            title=sheet.title or (entry.title if entry else None),
            new_revision=sheet.revision,
            new_path=sheet.abs_path,
            new_page=sheet.page_index,
            source_of_number=sheet.source_of_number,
            number_mismatch=sheet.number_mismatch,
            in_drawing_list=in_list,
            note="New drawing. It does not appear in the previous issue.",
            superseded_paths=[item.abs_path for item in new.superseded],
            duplicate_paths=[item.abs_path for item in new.duplicates],
        )
        if in_list is False:
            row.status = RegisterStatus.IN_FOLDER_NOT_IN_LIST
            row.note = (
                "This drawing is in the current issue but not on the drawing list. "
                "Query it with the design team."
            )
        return row

    assert old is not None
    sheet = old.best
    if issue_type is IssueType.PARTIAL:
        # Neutral language. Not reissued is not the same as deleted.
        status = RegisterStatus.NOT_REISSUED
        note = "Not reissued in this issue. Nothing to do."
    elif issue_type is IssueType.FULL:
        status = RegisterStatus.REMOVED
        note = (
            "In the previous issue but not in the current one. Confirm whether "
            "this drawing has been deleted from the scope."
        )
    else:
        status = RegisterStatus.NOT_REISSUED
        note = "Not present in the current issue."

    return RegisterRow(
        drawing_no=sheet.drawing_no or key,
        status=status,
        title=sheet.title or (entry.title if entry else None),
        old_revision=sheet.revision,
        old_path=sheet.abs_path,
        old_page=sheet.page_index,
        source_of_number=sheet.source_of_number,
        number_mismatch=sheet.number_mismatch,
        in_drawing_list=in_list,
        note=note,
        superseded_paths=[item.abs_path for item in old.superseded],
        duplicate_paths=[item.abs_path for item in old.duplicates],
    )


def _build_matched_row(key: str, old: _Group, new: _Group, in_list: bool | None) -> RegisterRow:
    """A drawing present in both issues: revised, unchanged, or reissued quietly."""
    old_sheet, new_sheet = old.best, new.best
    comparison = compare_revisions(old_sheet.revision, new_sheet.revision)
    same_content = (
        old_sheet.content_hash is not None
        and new_sheet.content_hash is not None
        and old_sheet.content_hash == new_sheet.content_hash
    )

    row = RegisterRow(
        drawing_no=new_sheet.drawing_no or old_sheet.drawing_no or key,
        status=RegisterStatus.UNCHANGED,
        title=new_sheet.title or old_sheet.title,
        old_revision=old_sheet.revision,
        new_revision=new_sheet.revision,
        old_path=old_sheet.abs_path,
        new_path=new_sheet.abs_path,
        old_page=old_sheet.page_index,
        new_page=new_sheet.page_index,
        source_of_number=new_sheet.source_of_number,
        number_mismatch=old_sheet.number_mismatch or new_sheet.number_mismatch,
        in_drawing_list=in_list,
        superseded_paths=[item.abs_path for item in (*old.superseded, *new.superseded)],
        duplicate_paths=[item.abs_path for item in (*old.duplicates, *new.duplicates)],
    )

    if comparison is RevisionComparison.STATUS_CHANGE:
        row.status = RegisterStatus.STATUS_CHANGE
        row.note = describe_change(old_sheet.revision, new_sheet.revision)
        return row

    if comparison is RevisionComparison.NEWER:
        row.status = RegisterStatus.REVISED
        row.note = describe_change(old_sheet.revision, new_sheet.revision)
        return row

    if comparison is RevisionComparison.OLDER:
        row.status = RegisterStatus.REVISED
        row.note = describe_change(old_sheet.revision, new_sheet.revision)
        row.needs_attention = True
        return row

    # Same revision, or no revision to go on. The file content decides.
    if same_content:
        row.status = RegisterStatus.UNCHANGED
        row.note = "Identical file in both issues. No comparison needed."
        return row

    if comparison is RevisionComparison.SAME:
        # ⚠ The finding a document controller is paid to catch.
        row.status = RegisterStatus.SAME_REV_DIFFERENT_FILE
        row.note = (
            f"Revision {new_sheet.revision} appears in both issues but the files "
            "differ. The drawing may have been reissued without a revision change. "
            "Compare it before signing anything off."
        )
        return row

    row.status = RegisterStatus.SAME_REV_DIFFERENT_FILE
    row.note = (
        "The files differ but the revision could not be read from one or both "
        "sheets, so the change cannot be confirmed from the title block. "
        "Compare them to see what changed."
    )
    return row


def _unidentified_rows(sheets: list[SheetRecord], side: Side) -> list[RegisterRow]:
    """Sheets with no drawing number, and sheets that could not be opened."""
    rows: list[RegisterRow] = []
    for sheet in sheets:
        is_old = side is Side.OLD
        if not sheet.is_readable:
            rows.append(
                RegisterRow(
                    drawing_no=sheet.filename,
                    status=RegisterStatus.UNREADABLE,
                    old_path=sheet.abs_path if is_old else None,
                    new_path=None if is_old else sheet.abs_path,
                    note=sheet.error_note or "This file could not be read.",
                )
            )
            continue

        rows.append(
            RegisterRow(
                drawing_no=sheet.filename,
                status=RegisterStatus.UNIDENTIFIED,
                title=sheet.title,
                old_revision=sheet.revision if is_old else None,
                new_revision=None if is_old else sheet.revision,
                old_path=sheet.abs_path if is_old else None,
                new_path=None if is_old else sheet.abs_path,
                old_page=sheet.page_index if is_old else None,
                new_page=None if is_old else sheet.page_index,
                note=(
                    "No drawing number could be read from this sheet. Enter the "
                    "number, or choose a sheet profile that matches this client."
                ),
            )
        )
    return rows


def _missing_from_folders(
    list_index: dict[str, ListEntry],
    old_index: dict[str, _Group],
    new_index: dict[str, _Group],
) -> list[RegisterRow]:
    """Drawings the list expects but neither folder contains."""
    rows: list[RegisterRow] = []
    for key, entry in list_index.items():
        if key in old_index or key in new_index:
            continue
        rows.append(
            RegisterRow(
                drawing_no=entry.drawing_no,
                status=RegisterStatus.IN_LIST_NOT_IN_FOLDER,
                title=entry.title,
                new_revision=entry.revision,
                in_drawing_list=True,
                source_of_number="drawing_list",
                note=(
                    "The drawing list expects this drawing but it is in neither "
                    "folder. Chase it with the design team."
                ),
            )
        )
    return rows
