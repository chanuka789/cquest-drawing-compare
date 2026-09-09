"""Execute a validated rename plan, one file at a time (Phase 3, Task 3.6).

This is the only part of the application that writes, moves or deletes a
user's files, so it is deliberately paranoid:

* It refuses to run a plan whose ``can_apply`` is False, naming the blocking
  statuses.
* In-place mode is opt-in: ``i_understand=True`` is required.
* In copy mode it refuses outright when any target lies inside an input
  folder — the app never writes into the folders it scanned.
* Every operation is recorded in the undo log **before** it happens; the log
  is flushed at the end (and on cancel) so completed work can always be
  reversed.
* Copy mode copies with :func:`shutil.copy2` and verifies the copy's hash
  against the source before recording success.
* In-place mode renames with :func:`os.replace` semantics and routes
  case-only renames through a temporary name, because Windows treats
  ``abc.pdf`` and ``ABC.pdf`` as the same file.
* Individual failures never abort the run: they are collected and reported,
  and cancel stops cleanly between files.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from engine.core.workspace import is_inside
from engine.ingest.file_hasher import content_hash
from engine.naming.rename_planner import (
    ActionStatus,
    RenameAction,
    RenameMode,
    RenamePlan,
)
from engine.naming.undo_log import UndoEntry, UndoLog, now_iso, safe_replace
from engine.utils.errors import UnreadableFileError
from engine.utils.longpath import long_path


class CancelTokenLike(Protocol):
    """Anything with a ``cancelled`` flag (e.g. engine.core.jobs.CancelToken)."""

    @property
    def cancelled(self) -> bool: ...


class VerificationError(Exception):
    """A copy did not match its source's content hash."""


@dataclass(slots=True)
class ExecutionResult:
    """What an apply run did: counts, failures and the log it wrote."""

    completed: int = 0
    failed: list[dict[str, object]] = field(default_factory=list)
    cancelled: bool = False
    undo_log_path: str | None = None
    duration_seconds: float = 0.0


def execute_plan(
    plan: RenamePlan,
    log_path: str | Path,
    *,
    mode: str = "copy",
    i_understand: bool = False,
    cancel: CancelTokenLike | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    input_folders: Sequence[str] = (),
) -> ExecutionResult:
    """Apply *plan*, recording every operation in an undo log at *log_path*."""
    requested = RenameMode(mode)
    if not plan.can_apply:
        raise ValueError(_refusal_message(plan))
    if requested is RenameMode.IN_PLACE and not i_understand:
        raise ValueError(
            "In-place renames change the files in the received issue folder. "
            "Pass i_understand=True to confirm, and the undo log will still be written."
        )
    if requested is not plan.mode:
        raise ValueError(
            f"The plan was built for mode {plan.mode.value!r} but execution was "
            f"requested in mode {requested.value!r}; rebuild the plan for that mode."
        )
    if requested is RenameMode.COPY:
        for action in plan.actions:
            if not action.target_path:
                continue
            for folder in input_folders:
                if folder and is_inside(action.target_path, folder):
                    raise ValueError(
                        "Copy-mode target inside an input folder; refusing to "
                        f"write there: {action.target_path}"
                    )

    to_do = [action for action in plan.actions if action.status is ActionStatus.OK]
    if requested is RenameMode.IN_PLACE:
        to_do = _order_in_place(to_do)

    undo_log = UndoLog(log_path)
    base_seq = len(undo_log.entries())
    result = ExecutionResult()
    started = time.perf_counter()

    for attempted, action in enumerate(to_do, start=1):
        if cancel is not None and cancel.cancelled:
            result.cancelled = True
            break

        name = Path(action.source_path).name or action.new_name
        entry = UndoEntry(
            seq=base_seq + attempted,
            operation=requested.value,
            source_path=str(action.source_path),
            target_path=str(action.target_path),
            source_hash="",
            success=False,
            timestamp=now_iso(),
        )
        # Log before acting: the entry exists before the file changes.
        undo_log.append(entry)

        note = "reading the source file"
        try:
            entry.source_hash = content_hash(action.source_path)
            if requested is RenameMode.COPY:
                note = "copying and verifying"
                _copy_verified(action, entry.source_hash)
            else:
                note = "moving into place"
                _move_in_place(action)
            entry.success = True
            result.completed += 1
        except (UnreadableFileError, VerificationError, OSError) as exc:
            result.failed.append({"path": action.source_path, "error": str(exc), "note": note})
        finally:
            if progress is not None:
                progress(attempted, len(to_do), name)

    undo_log.flush()
    result.undo_log_path = str(log_path)
    result.duration_seconds = time.perf_counter() - started
    return result


def _refusal_message(plan: RenamePlan) -> str:
    blocking = sorted(
        {
            action.status.value
            for action in plan.actions
            if action.status
            in (
                ActionStatus.COLLISION,
                ActionStatus.INVALID_NAME,
                ActionStatus.RESERVED_NAME,
                ActionStatus.PATH_TOO_LONG,
            )
        }
    )
    parts = list(plan.errors)
    if blocking:
        parts.append("unresolved: " + ", ".join(blocking))
    if not parts:
        parts.append("the plan contains blocked actions")
    return "Plan cannot be applied: " + "; ".join(parts)


def _copy_verified(action: RenameAction, source_hash: str) -> None:
    """Copy one action with :func:`shutil.copy2` and verify the copy."""
    target = Path(long_path(action.target_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(long_path(action.source_path), str(target))
    if content_hash(action.target_path) != source_hash:
        raise VerificationError(f"Copied file does not match its source: {action.target_path}")


def _move_in_place(action: RenameAction) -> None:
    """Move one action to its target, using a two-step rename when needed."""
    target = Path(long_path(action.target_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_replace(action.source_path, action.target_path)


def _order_in_place(actions: list[RenameAction]) -> list[RenameAction]:
    """Order renames so no target is still a pending source when it is used.

    ``B.pdf -> C.pdf`` must run before ``A.pdf -> B.pdf``, or the second
    move would overwrite ``B.pdf`` while it still holds the old content. A
    true cycle (``A -> B`` while ``B -> A``) cannot be reversed by this undo
    log safely, so it is refused rather than guessed at.
    """
    pending = list(actions)
    ordered: list[RenameAction] = []

    while pending:
        chosen = None
        for action in pending:
            target = os.path.normcase(os.path.abspath(action.target_path))
            others_move = {
                os.path.normcase(os.path.abspath(other.source_path))
                for other in pending
                if other is not action
            }
            if target not in others_move:
                chosen = action
                break
        if chosen is None:
            raise ValueError(
                "The in-place plan contains a rename cycle (files rename onto "
                "each other's current names); reorder the names and rebuild the plan."
            )
        ordered.append(chosen)
        pending.remove(chosen)
    return ordered
