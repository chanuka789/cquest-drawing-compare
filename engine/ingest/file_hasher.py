"""Content hashing, for true duplicate detection.

Two levels, because hashing 2 GB of A0 drawings twice is wasteful:

* :func:`quick_hash` reads the first and last 64 KB plus the size. Fast enough
  to run on everything, and good enough to say "these are definitely
  different". Use it to screen candidates.
* :func:`content_hash` reads the whole file in 1 MB chunks. Use it to confirm
  a duplicate, and as the identity that decides `unchanged` versus
  `same_rev_different_file` in reconciliation.

xxhash is used rather than MD5 because this is duplicate detection, not
security: xxh3 is roughly an order of magnitude faster and there is no
adversary trying to collide two drawings.
"""

from __future__ import annotations

import os
from pathlib import Path

import xxhash

from engine.utils.errors import UnreadableFileError
from engine.utils.longpath import long_path, safe_open

CHUNK_SIZE = 1024 * 1024  # 1 MB
EDGE_SIZE = 64 * 1024  # 64 KB from each end

#: Files at or below this size are hashed whole even by `quick_hash`.
SMALL_FILE_THRESHOLD = EDGE_SIZE * 2


def content_hash(path: str | Path) -> str:
    """Full-content hash. Two files with the same value are the same file."""
    digest = xxhash.xxh3_128()
    try:
        with safe_open(path, "rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        raise UnreadableFileError(
            "This file could not be read. It may be open in another program, "
            "or the network connection may have dropped.",
            detail={"path": str(path), "error": str(exc)},
        ) from exc
    return digest.hexdigest()


def quick_hash(path: str | Path, size: int | None = None) -> str:
    """Screening hash: size plus the first and last 64 KB.

    Different values guarantee different files. Equal values mean "probably
    the same" — confirm with :func:`content_hash` before acting on it.
    """
    target = long_path(path)
    if size is None:
        try:
            size = os.path.getsize(target)
        except OSError as exc:
            raise UnreadableFileError(
                "This file could not be read.",
                detail={"path": str(path), "error": str(exc)},
            ) from exc

    digest = xxhash.xxh3_128()
    digest.update(str(size).encode("ascii"))

    try:
        with safe_open(path, "rb") as handle:
            if size <= SMALL_FILE_THRESHOLD:
                digest.update(handle.read())
            else:
                digest.update(handle.read(EDGE_SIZE))
                handle.seek(-EDGE_SIZE, os.SEEK_END)
                digest.update(handle.read(EDGE_SIZE))
    except OSError as exc:
        raise UnreadableFileError(
            "This file could not be read. It may be open in another program.",
            detail={"path": str(path), "error": str(exc)},
        ) from exc

    return digest.hexdigest()


def find_duplicate_groups(paths: list[str]) -> dict[str, list[str]]:
    """Group paths by true content hash, returning only the groups with more than one member.

    Screens with :func:`quick_hash` first, so a full read only happens for
    files that could plausibly be duplicates.
    """
    by_quick: dict[str, list[str]] = {}
    for path in paths:
        try:
            by_quick.setdefault(quick_hash(path), []).append(path)
        except UnreadableFileError:
            continue  # quarantine handles these; a bad file is not a duplicate

    duplicates: dict[str, list[str]] = {}
    for candidates in by_quick.values():
        if len(candidates) < 2:
            continue
        for path in candidates:
            try:
                duplicates.setdefault(content_hash(path), []).append(path)
            except UnreadableFileError:
                continue

    return {key: group for key, group in duplicates.items() if len(group) > 1}
