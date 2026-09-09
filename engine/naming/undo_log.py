"""The undo log: the reversible record of every rename (Phase 3, Task 3.6).

Renaming is the most dangerous thing this application does, so every operation
is logged before it happens with everything needed to reverse it: sequence
number, operation type, source path, target path, the source file's content
hash, a success flag and a timestamp. The JSON file follows the
``_audit/rename_log.json`` layout from the Phase 3 plan — one file with an
``entries`` array — and the optional ``db_mirror`` callback hands each settled
entry to the caller (the API layer) so the same record can be mirrored into
the SQLite ``rename_log`` table without this module knowing anything about a
database.

Undo reverses completed entries in reverse order. With ``verify=True`` the
current file at the target is hashed first and compared with the recorded
source hash: if the file changed after the rename, undo stops and reports
rather than overwriting newer work. Reversing a copy-mode entry deletes only
the copy; the original source is never touched.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from engine.ingest.file_hasher import content_hash
from engine.utils.errors import UnreadableFileError
from engine.utils.longpath import long_path

#: Operation strings recorded in the log.
OPERATION_COPY = "copy"
OPERATION_IN_PLACE = "in_place"


class CancelTokenLike(Protocol):
    """Anything with a ``cancelled`` flag (e.g. engine.core.jobs.CancelToken)."""

    @property
    def cancelled(self) -> bool: ...


@dataclass(slots=True)
class UndoEntry:
    """One recorded operation, enough to reverse it."""

    seq: int
    operation: str
    source_path: str
    target_path: str
    source_hash: str
    success: bool
    timestamp: str  # ISO 8601, UTC


@dataclass(slots=True)
class UndoOutcome:
    """What an undo run managed to reverse, and why it stopped if it did."""

    reversed: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    stopped: bool = False
    reason: str | None = None


class UndoLog:
    """Rename log persisted to one JSON file, hydrated from it when present.

    The log is identified by its file path: constructing one over a file that
    already holds entries reads them in, so the API layer can open the
    ``_audit/rename_log.json`` of a finished apply run and reverse it. Entries
    are appended in memory and written by :meth:`flush`.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        db_mirror: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._entries: list[UndoEntry] = []
        self._db_mirror = db_mirror
        self._mirrored: set[int] = set()
        self._hydrate()

    def _hydrate(self) -> None:
        """Load entries already on disk, so an existing log can be undone."""
        if not self._path.is_file():
            return
        try:
            with open(Path(long_path(self._path)), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        self._entries = [UndoEntry(**item) for item in data.get("entries", [])]
        self._mirrored = {entry.seq for entry in self._entries if entry.success}

    # -- Recording --------------------------------------------------------

    def append(self, entry: UndoEntry) -> None:
        """Record *entry* in memory. Call before the operation it describes."""
        self._entries.append(entry)

    def entries(self) -> list[UndoEntry]:
        """The recorded entries, oldest first (by sequence)."""
        return list(self._entries)

    def flush(self) -> None:
        """Write the log to JSON and mirror settled entries via ``db_mirror``."""
        target = Path(long_path(self._path))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [self._to_dict(entry) for entry in self._entries],
        }
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

        if self._db_mirror is not None:
            for entry in self._entries:
                if entry.success and entry.seq not in self._mirrored:
                    self._db_mirror(self._to_dict(entry))
                    self._mirrored.add(entry.seq)

    @classmethod
    def load(cls, path: str | Path) -> UndoLog:
        """Read a previously flushed log back from disk."""
        log = cls(path)
        with open(Path(long_path(path)), encoding="utf-8") as handle:
            data = json.load(handle)
        log._entries = [UndoEntry(**item) for item in data.get("entries", [])]
        return log

    # -- Reversal ---------------------------------------------------------

    def undo(
        self,
        *,
        verify: bool = True,
        cancel: CancelTokenLike | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> UndoOutcome:
        """Reverse completed entries in reverse order.

        With ``verify=True`` each file is hashed before it is reversed; a
        mismatch with the recorded source hash stops the whole undo rather
        than overwriting newer work. Copy entries are undone by deleting the
        copy; in-place entries are moved back to their source path.
        """
        outcome = UndoOutcome()
        completed = [entry for entry in self._entries if entry.success]
        total = len(completed)

        for index, entry in enumerate(reversed(completed)):
            if cancel is not None and cancel.cancelled:
                outcome.stopped = True
                outcome.reason = "cancelled"
                break
            if progress is not None:
                progress(index, total)
            if verify:
                try:
                    current = content_hash(entry.target_path)
                except UnreadableFileError:
                    outcome.stopped = True
                    outcome.reason = "file missing or unreadable since rename"
                    outcome.failed.append(
                        {
                            "path": entry.target_path,
                            "reason": "file is missing or unreadable; "
                            "cannot verify before reversing",
                        }
                    )
                    break
                if current != entry.source_hash:
                    outcome.stopped = True
                    outcome.reason = "file changed since rename"
                    outcome.failed.append(
                        {
                            "path": entry.target_path,
                            "reason": "file changed since rename; refusing to overwrite newer work",
                        }
                    )
                    break
            try:
                if entry.operation == OPERATION_COPY:
                    os.remove(long_path(entry.target_path))
                elif entry.operation == OPERATION_IN_PLACE:
                    _replace_file(entry.target_path, entry.source_path)
                else:
                    outcome.failed.append(
                        {
                            "path": entry.source_path,
                            "reason": f"unknown operation {entry.operation!r}",
                        }
                    )
                    continue
            except OSError as exc:
                outcome.failed.append(
                    {
                        "path": entry.source_path,
                        "reason": f"could not reverse: {exc}",
                    }
                )
                continue
            outcome.reversed += 1

        return outcome

    # -- Serialisation ----------------------------------------------------

    @staticmethod
    def _to_dict(entry: UndoEntry) -> dict[str, object]:
        return {
            "seq": entry.seq,
            "operation": entry.operation,
            "source_path": entry.source_path,
            "target_path": entry.target_path,
            "source_hash": entry.source_hash,
            "success": entry.success,
            "timestamp": entry.timestamp,
        }


def safe_replace(source: str | Path, target: str | Path) -> None:
    """Move a file, surviving a case-only rename on Windows.

    Windows treats ``abc.pdf`` and ``ABC.pdf`` as the same file, so renaming
    one to the other goes through a temporary name in two steps. Otherwise
    this is a plain :func:`os.replace`.
    """
    _replace_file(source, target)


def _replace_file(source: str | Path, target: str | Path) -> None:
    source_text = os.path.abspath(str(source))
    target_text = os.path.abspath(str(target))
    if (
        os.path.normcase(source_text) == os.path.normcase(target_text)
        and source_text != target_text
    ):
        temporary = os.path.join(os.path.dirname(target_text), f".cqdc-tmp-{uuid.uuid4().hex}")
        os.replace(long_path(source), long_path(temporary))
        try:
            os.replace(long_path(temporary), long_path(target))
        except BaseException:
            os.replace(long_path(temporary), long_path(source))
            raise
    else:
        os.replace(long_path(source), long_path(target))


def now_iso() -> str:
    """Current time as an ISO 8601 UTC string for a log entry."""
    return datetime.now(UTC).isoformat()
