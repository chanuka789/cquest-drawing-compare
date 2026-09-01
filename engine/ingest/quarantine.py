"""The quarantine list: files that could not be read, and why.

The rule this module exists to enforce: **one bad file never stops the batch.**
A folder of 300 drawings routinely contains two that are password-protected
and one that was truncated by a failed copy. The run must complete, and the
user must be told exactly which files were left out and what to do about each.

Every reason here is written for the user, not for a developer. "Corrupt file"
is not an instruction; "Try re-downloading it from the source" is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from loguru import logger


class QuarantineReason(StrEnum):
    """Why a file was set aside. Drives the wording and the suggested action."""

    PASSWORD_PROTECTED = "password_protected"
    DAMAGED = "damaged"
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    NOT_A_PDF = "not_a_pdf"
    TOO_LARGE = "too_large"


#: What the user should do next, per reason.
ADVICE: dict[QuarantineReason, str] = {
    QuarantineReason.PASSWORD_PROTECTED: (
        "Ask the sender for a copy without a password, then scan again."
    ),
    QuarantineReason.DAMAGED: (
        "Re-download the file from the source and scan again. If it still fails, "
        "the copy you were sent is broken."
    ),
    QuarantineReason.EMPTY: "Check whether the file finished copying, then scan again.",
    QuarantineReason.UNREADABLE: (
        "Check the file is not open in another program and that the network drive "
        "is still connected."
    ),
    QuarantineReason.NOT_A_PDF: "Only PDF drawings are read. Convert the file, or exclude it.",
    QuarantineReason.TOO_LARGE: "Split the file, or raise the size limit in Settings.",
}

SHORT_LABEL: dict[QuarantineReason, str] = {
    QuarantineReason.PASSWORD_PROTECTED: "Password-protected",
    QuarantineReason.DAMAGED: "Damaged",
    QuarantineReason.EMPTY: "Empty",
    QuarantineReason.UNREADABLE: "Could not be read",
    QuarantineReason.NOT_A_PDF: "Not a PDF",
    QuarantineReason.TOO_LARGE: "Too large",
}


@dataclass(frozen=True, slots=True)
class QuarantinedFile:
    """One file that was set aside, with a reason a user can act on."""

    path: str
    filename: str
    reason: QuarantineReason
    detail: str

    @property
    def label(self) -> str:
        return SHORT_LABEL[self.reason]

    @property
    def advice(self) -> str:
        return ADVICE[self.reason]

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "filename": self.filename,
            "reason": str(self.reason),
            "label": self.label,
            "detail": self.detail,
            "advice": self.advice,
        }


@dataclass(slots=True)
class Quarantine:
    """Collects unreadable files during a run."""

    entries: list[QuarantinedFile] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def add(self, path: str | Path, reason: QuarantineReason, detail: str = "") -> QuarantinedFile:
        """Record one file. Always returns; never raises."""
        entry = QuarantinedFile(
            path=str(path),
            filename=Path(str(path)).name,
            reason=reason,
            detail=detail or SHORT_LABEL[reason],
        )
        self.entries.append(entry)
        logger.info("Quarantined | {} | {}", entry.filename, reason)
        return entry

    def add_from_inspection(self, info: object) -> QuarantinedFile | None:
        """Quarantine a :class:`~engine.ingest.pdf_inspector.PdfInfo` if it failed."""
        from engine.ingest.pdf_inspector import PdfInfo

        if not isinstance(info, PdfInfo) or info.is_readable:
            return None

        if info.needs_password:
            reason = QuarantineReason.PASSWORD_PROTECTED
        elif info.size == 0 or (
            info.page_count == 0 and info.error_note and "no pages" in info.error_note
        ):
            reason = QuarantineReason.EMPTY
        else:
            reason = QuarantineReason.DAMAGED

        return self.add(info.path, reason, info.error_note or "")

    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[str(entry.reason)] = counts.get(str(entry.reason), 0) + 1
        return counts

    def summary(self) -> str:
        """One sentence for the UI. Says what happened, not just a number."""
        if not self.entries:
            return "Every file was read successfully."

        count = len(self.entries)
        noun = "file" if count == 1 else "files"
        parts = [
            f"{number} {SHORT_LABEL[QuarantineReason(reason)].lower()}"
            for reason, number in self.by_reason().items()
        ]
        return f"{count} {noun} could not be read: {', '.join(parts)}."

    def as_list(self) -> list[dict[str, str]]:
        return [entry.as_dict() for entry in self.entries]
