"""Plan a bulk rename before anything touches the disk (Phase 3, Task 3.5).

The rename screen shows the user a full plan and nothing is written until they
apply it, so the plan is where every safety check lives. For each sheet the
planner renders a target name with :func:`engine.naming.template.render` and
then runs the Windows trap checks from the Phase 3 plan:

* the source file actually exists;
* the target stem is not a reserved device name (``CON``, ``PRN``, ``AUX``,
  ``NUL``, ``COM1``-``COM9``, ``LPT1``-``LPT9``);
* no invalid character or trailing dot survived sanitising;
* the full final path is not absurdly long;
* the target is not already occupied on disk or by another row of the same
  plan — collisions are resolved with ``_2``/``_3`` suffixes, never by
  overwriting;
* the source is not locked by another program.

Statuses are plain strings (a :class:`StrEnum`), stored as JSON-able values so
the plan can cross the API boundary without conversion. ``can_apply`` is False
when any unresolved collision, invalid or reserved name, or over-long path is
present, or when the plan carries errors — the executor refuses those plans.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from engine.core.models import SheetRecord
from engine.naming.normaliser import FILE_EXTENSIONS
from engine.naming.template import is_reserved_name, parse_drawing_number, render
from engine.utils.longpath import long_path

#: Absolute ceiling for one full path. A ``\\?\``-prefixed Windows path may be
#: up to 32767 characters; 32000 leaves margin for a name appended by a later
#: tool. Documented so nobody "fixes" it back down to 260.
MAX_PATH = 32000

#: The classic Windows ``MAX_PATH`` the application is exempt from because
#: every file access goes through :mod:`engine.utils.longpath`.
LEGACY_MAX_PATH = 259

#: Long paths are always available here: CLAUDE.md routes every file access
#: through ``engine/utils/longpath.py``, which adds the ``\\?\`` prefix, so an
#: over-260-character path is a warning, and only a path past
#: :data:`MAX_PATH` becomes a blocking :attr:`ActionStatus.PATH_TOO_LONG`.
LONG_PATHS_ENABLED = True

#: One-level discipline folder per ISO 19650 role code (Phase 3 plan, B8).
ROLE_FOLDERS: dict[str, str] = {
    "A": "Architectural",
    "S": "Structural",
    "M": "Mechanical",
    "E": "Electrical",
    "P": "Plumbing",
    "L": "Landscape",
}

#: Collision suffixes stop being tried after this many attempts.
_MAX_SUFFIX = 1000

#: Characters Windows never allows inside a file name.
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class RenameMode(StrEnum):
    """Where a rename writes its result."""

    COPY = "copy"
    IN_PLACE = "in_place"


class ActionStatus(StrEnum):
    """The verdict for one planned rename, stored as a plain string."""

    OK = "ok"
    UNCHANGED = "unchanged"
    COLLISION = "collision"
    INVALID_NAME = "invalid_name"
    PATH_TOO_LONG = "path_too_long"
    MISSING_TOKENS = "missing_tokens"
    LOCKED = "locked"
    RESERVED_NAME = "reserved_name"
    SOURCE_MISSING = "source_missing"


#: Statuses that stop a plan from being applied. Warnings alone never block.
_BLOCKING_STATUSES: frozenset[ActionStatus] = frozenset(
    {
        ActionStatus.COLLISION,
        ActionStatus.INVALID_NAME,
        ActionStatus.RESERVED_NAME,
        ActionStatus.PATH_TOO_LONG,
    }
)


@dataclass(slots=True)
class RenameAction:
    """One proposed rename: what is copied or moved, and to where."""

    source_path: str
    target_path: str = ""
    target_folder: str = ""
    status: ActionStatus = ActionStatus.OK
    old_name: str = ""
    new_name: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RenamePlan:
    """A dry run: every action, the counts, and whether it may be applied."""

    actions: list[RenameAction]
    mode: RenameMode
    output_dir: str
    summary: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    #: Plan-level notes that do not block anything (e.g. skipped files).
    warnings: list[str] = field(default_factory=list)

    @property
    def can_apply(self) -> bool:
        """False when anything would have to be overwritten, invented or
        truncated to proceed, or when the plan carries errors."""
        if self.errors:
            return False
        return not any(action.status in _BLOCKING_STATUSES for action in self.actions)


def build_plan(
    sheets: Sequence[SheetRecord],
    template_text: str,
    context: dict[str, str],
    output_dir: str | Path,
    *,
    mode: str | RenameMode = RenameMode.COPY,
    use_discipline_folders: bool = False,
    parse_pattern: str | None = None,
) -> RenamePlan:
    """Dry-run *sheets* against *template_text* and return the full plan.

    ``output_dir`` is where copies land in copy mode (the plan never creates
    it). In in-place mode each file is renamed inside its own folder and
    ``output_dir`` is only carried as metadata.
    """
    plan_mode = RenameMode(mode)
    out_root = Path(output_dir) if str(output_dir) else Path(".")
    actions: list[RenameAction] = []
    skipped: list[str] = []

    for sheet in sheets:
        source = Path(sheet.abs_path)
        extension = source.suffix.lower().lstrip(".")
        if extension not in FILE_EXTENSIONS:
            skipped.append(
                f"Skipped {source.name or sheet.filename}: not a supported "
                f"drawing file (.{extension or 'none'})."
            )
            continue

        action = RenameAction(
            source_path=str(source),
            old_name=sheet.filename or source.name,
        )

        if not source.is_file():
            action.status = ActionStatus.SOURCE_MISSING
            action.warnings.append(f"Source file not found: {source}")
            actions.append(action)
            continue

        result = render(template_text, sheet, context, parse_pattern=parse_pattern)
        if result.empty:
            action.status = ActionStatus.MISSING_TOKENS
            missing = ", ".join(f"{{{name}}}" for name in result.missing)
            action.warnings.append(
                "No usable file name could be built for this sheet"
                f"{f'; could not resolve {missing}' if missing else ' from this template'}."
            )
            actions.append(action)
            continue

        new_filename = result.filename
        if result.warning is not None:
            action.warnings.append(result.warning)

        folder = ""
        if plan_mode is RenameMode.COPY and use_discipline_folders:
            role = parse_drawing_number(sheet.drawing_no, parse_pattern).get("role")
            folder = ROLE_FOLDERS.get(role.upper() if role else "", "")
        action.target_folder = folder

        base_dir = out_root / folder if folder else out_root
        if plan_mode is RenameMode.IN_PLACE:
            base_dir = source.parent
        target = base_dir / new_filename
        action.target_path = str(target)
        action.new_name = new_filename

        name_suffix = Path(sheet.filename).suffix
        if name_suffix and new_filename.endswith(name_suffix):
            stem = new_filename[: -len(name_suffix)]
        else:
            stem = Path(new_filename).stem

        if is_reserved_name(stem):
            action.status = ActionStatus.RESERVED_NAME
            action.warnings.append(
                f"{stem} is a reserved Windows device name and cannot be used "
                "as a file name; rename it in the review before applying."
            )
            actions.append(action)
            continue

        if new_filename.endswith(".") or _INVALID_CHARS.search(new_filename) is not None:
            action.status = ActionStatus.INVALID_NAME
            action.warnings.append(
                f"{new_filename!r} still contains characters Windows does not allow in a file name."
            )
            actions.append(action)
            continue

        target_abs = os.path.abspath(action.target_path)
        length = len(target_abs)
        limit = MAX_PATH if LONG_PATHS_ENABLED else LEGACY_MAX_PATH
        if length > limit:
            action.status = ActionStatus.PATH_TOO_LONG
            action.warnings.append(
                f"The full target path is {length} characters, longer than the "
                f"allowed limit of {limit}; shorten the output folder or the name."
            )
            actions.append(action)
            continue
        if LONG_PATHS_ENABLED and length > LEGACY_MAX_PATH:
            action.warnings.append(
                f"The full target path is {length} characters, over the classic "
                "260-character Windows limit; allowed because long paths are enabled."
            )

        source_abs = os.path.abspath(str(source))
        if target_abs == source_abs:
            action.status = ActionStatus.UNCHANGED
            actions.append(action)
            continue

        if plan_mode is RenameMode.IN_PLACE and os.path.normcase(target_abs) == os.path.normcase(
            source_abs
        ):
            action.warnings.append("case-only rename (Windows needs a two-step rename)")
        else:
            try:
                _can_open(source)
            except (PermissionError, OSError):
                action.status = ActionStatus.LOCKED
                action.warnings.append(f"Source file is locked or cannot be opened: {source}")
        actions.append(action)

    _resolve_conflicts(actions)

    plan = RenamePlan(actions=actions, mode=plan_mode, output_dir=str(output_dir))
    plan.warnings = skipped
    plan.summary = _summarise(actions)

    if plan_mode is RenameMode.COPY:
        _check_free_space(plan)

    return plan


def _can_open(path: str | Path) -> None:
    """Open *path* for reading and close it again.

    Raises :class:`PermissionError` or :class:`OSError` when the file is
    locked by another program. Kept as its own module-level function so tests
    can monkeypatch it to simulate a locked file.
    """
    with open(long_path(path), "rb"):
        pass


def _resolve_conflicts(actions: list[RenameAction]) -> None:
    """Give colliding rows distinct targets; never overwrite anything.

    Two rows whose targets collide (or a target that already exists on disk)
    get ``_2``, ``_3`` … appended before the extension. The first row keeps
    the base name; later rows are marked OK with a warning that spells out
    what happened. A target that an earlier row is going to vacate in-place is
    not treated as a collision, so rename chains survive.
    """
    by_source: dict[str, RenameAction] = {}
    for action in actions:
        if action.source_path:
            by_source.setdefault(_key(action.source_path), action)

    claimed: dict[str, RenameAction] = {}
    for action in actions:
        if action.status not in (ActionStatus.OK, ActionStatus.UNCHANGED):
            continue
        if not action.target_path:
            continue
        target = Path(action.target_path)
        source_key = _key(action.source_path)
        target_key = _key(action.target_path)

        conflict: str | None = None
        if target_key in claimed:
            conflict = "row"
        elif target_key != source_key and _exists(target):
            occupant = by_source.get(target_key)
            if occupant is None or not _vacates(occupant):
                conflict = "disk"

        if conflict is None:
            claimed[target_key] = action
            continue

        final: Path | None = None
        index = 2
        while index <= _MAX_SUFFIX:
            candidate = _suffixed(target, index)
            candidate_key = _key(str(candidate))
            if candidate_key not in claimed and (
                candidate_key == source_key or not _exists(candidate)
            ):
                final = candidate
                break
            index += 1

        if final is None:
            action.status = ActionStatus.COLLISION
            action.warnings.append(
                f"Could not find a free target name for {target.name}; check the plan."
            )
            continue

        if conflict == "row":
            action.warnings.append(
                f"two files would share the name {target.name}; renamed to {final.name}"
            )
        else:
            action.warnings.append(
                f"{target.name} already exists on disk; will not overwrite; renamed to {final.name}"
            )
        action.target_path = str(final)
        action.new_name = final.name
        claimed[_key(action.target_path)] = action


def _vacates(action: RenameAction) -> bool:
    """True when executing *action* removes its file from its source location.

    Case-only and unchanged rows keep their location occupied, so another row
    may not move onto it.
    """
    if action.status is not ActionStatus.OK or not action.target_path:
        return False
    return _key(action.source_path) != _key(action.target_path)


def _suffixed(target: Path, index: int) -> Path:
    """``A-101.pdf`` with index 2 -> ``A-101_2.pdf`` (before the extension)."""
    return target.with_name(f"{target.stem}_{index}{target.suffix}")


def _key(path: str | Path) -> str:
    """Case-insensitive absolute key for comparing file locations."""
    return os.path.normcase(os.path.abspath(str(path)))


def _exists(path: Path) -> bool:
    return os.path.exists(long_path(path))


def _summarise(actions: list[RenameAction]) -> dict[str, int]:
    counts = {status.value: 0 for status in ActionStatus}
    for action in actions:
        counts[action.status.value] += 1
    summary = dict(counts)
    summary["to_change"] = counts[ActionStatus.OK.value]
    summary["unchanged"] = counts[ActionStatus.UNCHANGED.value]
    summary["problems"] = sum(
        counts[status.value]
        for status in ActionStatus
        if status not in (ActionStatus.OK, ActionStatus.UNCHANGED)
    )
    return summary


def _check_free_space(plan: RenamePlan) -> None:
    """Copy mode must fit on the output drive, with a 10% margin."""
    if not plan.actions:
        return
    total = 0
    for action in plan.actions:
        if action.status is not ActionStatus.OK:
            continue
        try:
            total += os.path.getsize(long_path(action.source_path))
        except OSError:
            continue
    if not total:
        return
    free = _free_space(plan.output_dir)
    needed = int(total * 1.1)
    if free is None:
        plan.errors.append(
            f"Cannot check the free space on {plan.output_dir or '.'} for the copies."
        )
    elif needed > free:
        plan.errors.append(
            f"Not enough free space to copy {total} bytes into "
            f"{plan.output_dir or '.'}: about {needed} bytes are needed "
            f"but only {free} are free."
        )


def _free_space(folder: str | Path) -> int | None:
    """Free bytes on the drive holding *folder* (or its nearest ancestor)."""
    candidate = Path(str(folder) or ".")
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    try:
        return shutil.disk_usage(long_path(candidate)).free
    except OSError:
        return None
