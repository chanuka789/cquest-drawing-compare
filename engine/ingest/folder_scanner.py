"""Stage 1, pass 1: read the folder using only what the operating system knows.

This pass never opens a PDF. It reports filename, size, modified time and path,
which `os.scandir` gives us almost for free, so the file list can be on screen
in under two seconds while the deep pass runs behind it.

Three things that break naive folder walks on real project shares, and how
they are handled here:

* Paths longer than 260 characters — every path goes through
  :mod:`engine.utils.longpath`.
* Symlinks and directory junctions — network shares contain loops, so links
  are never followed.
* Per-file permission errors — one unreadable file must not end the walk.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from engine.utils.errors import NotFoundError, ValidationError
from engine.utils.longpath import long_path, strip_prefix

#: Files that are never drawings, whatever folder they turn up in.
DEFAULT_IGNORE_FILES: frozenset[str] = frozenset(
    {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db", "icon\r"}
)

#: Filename prefixes to skip. `~$` is an open Office lock file.
DEFAULT_IGNORE_PREFIXES: tuple[str, ...] = ("~$", ".~")

#: Folder names that hold superseded material. Skipped whole, and reported.
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {"_archive", "archive", "superseded", "old", "obsolete", "backup", "$recycle.bin"}
)

#: Extensions treated as drawings for this phase.
DEFAULT_DRAWING_EXTENSIONS: frozenset[str] = frozenset({".pdf"})

#: Extensions worth telling the user about even though Phase 2 cannot read them.
NOTABLE_OTHER_EXTENSIONS: frozenset[str] = frozenset(
    {".dwg", ".dxf", ".dwf", ".rvt", ".ifc", ".xlsx", ".xls", ".csv", ".docx", ".doc"}
)


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One file found on disk. Nothing here required opening the file."""

    abs_path: str
    rel_path: str
    filename: str
    extension: str
    size: int
    modified: datetime
    depth: int

    @property
    def is_drawing(self) -> bool:
        return self.extension in DEFAULT_DRAWING_EXTENSIONS

    @property
    def cache_key(self) -> tuple[str, int, float]:
        """Identity for the scan cache: path, size and modified time."""
        return (self.abs_path, self.size, self.modified.timestamp())


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    """Something deliberately not scanned, kept so the user can see why."""

    path: str
    reason: str


@dataclass(slots=True)
class ScanOptions:
    """What to include and exclude. Every rule here is user-visible."""

    ignore_files: frozenset[str] = DEFAULT_IGNORE_FILES
    ignore_prefixes: tuple[str, ...] = DEFAULT_IGNORE_PREFIXES
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS
    drawing_extensions: frozenset[str] = DEFAULT_DRAWING_EXTENSIONS
    skip_hidden: bool = True
    follow_links: bool = False
    #: Guards against a pathological tree. Real sets rarely go past 12 deep.
    max_depth: int = 32


@dataclass(slots=True)
class ScanResult:
    """Everything pass 1 learned about a folder."""

    root: str
    drawings: list[ScannedFile] = field(default_factory=list)
    other_files: list[ScannedFile] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)
    errors: list[SkippedEntry] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def drawing_count(self) -> int:
        return len(self.drawings)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.drawings)

    def other_by_extension(self) -> dict[str, int]:
        """Counts of the non-drawing files worth mentioning, e.g. DWGs."""
        counts: dict[str, int] = {}
        for item in self.other_files:
            if item.extension in NOTABLE_OTHER_EXTENSIONS:
                counts[item.extension] = counts.get(item.extension, 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: -pair[1]))


def _is_hidden(entry: os.DirEntry[str]) -> bool:
    """Windows hidden or system attribute, without a second stat call."""
    if entry.name.startswith("."):
        return True
    try:
        attributes = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    hidden_or_system = 0x2 | 0x4
    return bool(attributes & hidden_or_system)


def _should_skip_file(name: str, options: ScanOptions) -> str | None:
    """Return the reason this file is skipped, or None to keep it."""
    lowered = name.lower()
    if lowered in options.ignore_files:
        return "System file"
    if name.startswith(options.ignore_prefixes):
        return "Temporary file left open by another program"
    return None


def _walk(
    directory: str,
    root: str,
    depth: int,
    options: ScanOptions,
    result: ScanResult,
) -> Iterator[ScannedFile]:
    """Depth-first walk that survives permission errors on any single entry."""
    if depth > options.max_depth:
        result.skipped.append(SkippedEntry(strip_prefix(directory), "Folder nesting too deep"))
        return

    try:
        entries = list(os.scandir(directory))
    except PermissionError:
        result.errors.append(
            SkippedEntry(
                strip_prefix(directory),
                "You do not have permission to read this folder. Ask for access, "
                "or choose a different folder.",
            )
        )
        return
    except OSError as exc:
        result.errors.append(
            SkippedEntry(strip_prefix(directory), f"This folder could not be read: {exc.strerror}")
        )
        return

    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=options.follow_links)
        except OSError:
            continue

        if not options.follow_links and entry.is_symlink():
            result.skipped.append(SkippedEntry(strip_prefix(entry.path), "Shortcut or junction"))
            continue

        if is_dir:
            if entry.name.lower() in options.ignore_dirs:
                result.skipped.append(
                    SkippedEntry(strip_prefix(entry.path), f"Folder named '{entry.name}'")
                )
                continue
            if options.skip_hidden and _is_hidden(entry):
                result.skipped.append(SkippedEntry(strip_prefix(entry.path), "Hidden folder"))
                continue
            yield from _walk(entry.path, root, depth + 1, options, result)
            continue

        reason = _should_skip_file(entry.name, options)
        if reason:
            result.skipped.append(SkippedEntry(strip_prefix(entry.path), reason))
            continue
        if options.skip_hidden and _is_hidden(entry):
            result.skipped.append(SkippedEntry(strip_prefix(entry.path), "Hidden file"))
            continue

        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            result.errors.append(
                SkippedEntry(
                    strip_prefix(entry.path),
                    f"This file could not be read: {exc.strerror}",
                )
            )
            continue

        absolute = strip_prefix(entry.path)
        yield ScannedFile(
            abs_path=absolute,
            rel_path=os.path.relpath(absolute, root),
            filename=entry.name,
            extension=os.path.splitext(entry.name)[1].lower(),
            size=info.st_size,
            modified=datetime.fromtimestamp(info.st_mtime, tz=UTC),
            depth=depth,
        )


def scan_folder(root: str | Path, options: ScanOptions | None = None) -> ScanResult:
    """Walk *root* recursively and return every file worth knowing about.

    Fast by design: no file is opened, so this is bounded by directory
    metadata reads. Target is under two seconds for 300 files locally.
    """
    options = options or ScanOptions()
    root_display = strip_prefix(os.path.abspath(str(root)))

    if not os.path.isdir(long_path(root_display)):
        if os.path.exists(long_path(root_display)):
            raise ValidationError(
                "That is a file, not a folder. Choose the folder that holds the drawings.",
                detail={"path": root_display},
            )
        raise NotFoundError(
            "That folder could not be found. It may have been moved, renamed, "
            "or the network drive may be disconnected.",
            detail={"path": root_display},
        )

    started = time.perf_counter()
    result = ScanResult(root=root_display)

    walk_root = long_path(root_display, force=True)
    for item in _walk(walk_root, root_display, 0, options, result):
        if item.extension in options.drawing_extensions:
            result.drawings.append(item)
        else:
            result.other_files.append(item)

    result.drawings.sort(key=lambda item: item.rel_path.lower())
    result.other_files.sort(key=lambda item: item.rel_path.lower())
    result.duration_seconds = time.perf_counter() - started

    logger.info(
        "Scanned {} | {} drawings, {} other, {} skipped, {} errors in {:.2f}s",
        root_display,
        len(result.drawings),
        len(result.other_files),
        len(result.skipped),
        len(result.errors),
        result.duration_seconds,
    )
    return result
