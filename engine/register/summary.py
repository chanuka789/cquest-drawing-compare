"""Set-level counts, and one sentence a person can actually read.

The summary line is what goes in the email and at the top of the Excel
export, so it is written the way a document controller would say it, and it
uses the neutral wording for a partial issue.
"""

from __future__ import annotations

from collections.abc import Sequence

from engine.core.enums import IssueType, RegisterStatus
from engine.core.models import RegisterRow, RegisterSummary

#: How each status reads in a sentence, singular and plural.
STATUS_WORDS: dict[RegisterStatus, tuple[str, str]] = {
    RegisterStatus.REVISED: ("revised", "revised"),
    RegisterStatus.UNCHANGED: ("unchanged", "unchanged"),
    RegisterStatus.SAME_REV_DIFFERENT_FILE: (
        "same revision but a different file",
        "same revision but a different file",
    ),
    RegisterStatus.NEW: ("new", "new"),
    RegisterStatus.NOT_REISSUED: ("not reissued", "not reissued"),
    RegisterStatus.REMOVED: ("removed", "removed"),
    RegisterStatus.STATUS_CHANGE: ("status change", "status changes"),
    RegisterStatus.SUPERSEDED_IN_FOLDER: ("superseded in folder", "superseded in folder"),
    RegisterStatus.DUPLICATE_FILE: ("duplicate file", "duplicate files"),
    RegisterStatus.UNIDENTIFIED: ("unidentified", "unidentified"),
    RegisterStatus.UNREADABLE: ("could not be read", "could not be read"),
    RegisterStatus.IN_LIST_NOT_IN_FOLDER: ("on the list but missing", "on the list but missing"),
    RegisterStatus.IN_FOLDER_NOT_IN_LIST: ("not on the drawing list", "not on the drawing list"),
}

#: The order counts are read out in: the interesting things first.
SENTENCE_ORDER: tuple[RegisterStatus, ...] = (
    RegisterStatus.REVISED,
    RegisterStatus.STATUS_CHANGE,
    RegisterStatus.NEW,
    RegisterStatus.SAME_REV_DIFFERENT_FILE,
    RegisterStatus.REMOVED,
    RegisterStatus.NOT_REISSUED,
    RegisterStatus.UNCHANGED,
    RegisterStatus.IN_LIST_NOT_IN_FOLDER,
    RegisterStatus.IN_FOLDER_NOT_IN_LIST,
    RegisterStatus.UNIDENTIFIED,
    RegisterStatus.UNREADABLE,
)


def build_summary(
    rows: Sequence[RegisterRow],
    old_sheet_count: int,
    new_sheet_count: int,
    issue_type: IssueType = IssueType.UNKNOWN,
) -> RegisterSummary:
    """Count the register and describe it in one sentence."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.status)] = counts.get(str(row.status), 0) + 1

    attention = sum(1 for row in rows if row.needs_attention)
    comparable = sum(1 for row in rows if row.is_comparable)

    return RegisterSummary(
        counts=counts,
        old_sheet_count=old_sheet_count,
        new_sheet_count=new_sheet_count,
        issue_type=issue_type,
        attention_count=attention,
        comparable_count=comparable,
        sentence=build_sentence(counts, attention, issue_type),
    )


def build_sentence(
    counts: dict[str, int], attention: int, issue_type: IssueType = IssueType.UNKNOWN
) -> str:
    """A readable summary, e.g. "12 revised, 3 new, 188 not reissued"."""
    parts: list[str] = []
    for status in SENTENCE_ORDER:
        count = counts.get(str(status), 0)
        if not count:
            continue
        singular, plural = STATUS_WORDS[status]
        parts.append(f"{count} {singular if count == 1 else plural}")

    if not parts:
        return "No drawings were found in either folder."

    sentence = ", ".join(parts) + "."

    if issue_type is IssueType.PARTIAL:
        sentence += " This is a partial issue, so drawings that were not reissued need no action."
    elif issue_type is IssueType.UNKNOWN:
        sentence += " The issue type has not been confirmed."

    if attention:
        noun = "drawing needs" if attention == 1 else "drawings need"
        sentence += f" {attention} {noun} attention."

    return sentence
