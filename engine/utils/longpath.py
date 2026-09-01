r"""Windows long path handling.

Why this file exists
--------------------
The Win32 API limits a path to 260 characters (MAX_PATH). Real construction
drawing folders break that limit routinely:

    \\server\Projects\2026\UVU Jeddah Tower\03_Drawings\Architectural\IFC\
    Rev D\Level 03\UVU-KEO-ARC-L03-DR-A-001234-Rev-D-Reflected-Ceiling-Plan.pdf

Enabling `LongPathsEnabled` in the registry helps, but it does not cover every
API on every machine, and it does nothing for a client who has not set it. The
reliable fix is the `\\?\` prefix, which tells Windows to skip path parsing and
the length check entirely.

Two rules for the prefixed form:

* The path must be absolute and fully normalised — no `.`, no `..`, no forward
  slashes. Windows does no parsing at all on a prefixed path.
* A UNC path takes a different prefix: `\\server\share` becomes
  `\\?\UNC\server\share`.

Every file access in the engine that touches a user-supplied drawing path
should go through :func:`safe_open` or prefix with :func:`long_path` first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Any

#: Prefix a path once it gets this long. MAX_PATH is 260; the margin leaves
#: room for a filename to be appended to a directory path by the caller.
LONG_PATH_THRESHOLD = 240

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def is_windows() -> bool:
    """True on Windows, where the prefix means something."""
    return sys.platform == "win32"


def has_prefix(path: str | os.PathLike[str]) -> bool:
    """True if *path* already carries the `\\\\?\\` prefix."""
    return str(path).startswith(_PREFIX)


def long_path(path: str | os.PathLike[str], *, force: bool = False) -> str:
    """Return *path* as a string safe to hand to the Windows file APIs.

    Adds the `\\\\?\\` prefix when the absolute, normalised path exceeds
    :data:`LONG_PATH_THRESHOLD` characters. Pass ``force=True`` to prefix
    regardless of length.

    On non-Windows platforms, and for paths that are already prefixed, the
    input is returned unchanged apart from normalisation.
    """
    text = str(path)

    if not is_windows() or has_prefix(text):
        return text

    absolute = os.path.abspath(text)  # normalises separators, `.` and `..`

    if not force and len(absolute) <= LONG_PATH_THRESHOLD:
        return absolute

    if absolute.startswith("\\\\"):
        # UNC share: \\server\share\... -> \\?\UNC\server\share\...
        return _UNC_PREFIX + absolute[2:]
    return _PREFIX + absolute


def strip_prefix(path: str | os.PathLike[str]) -> str:
    """Remove the `\\\\?\\` prefix, for display or logging.

    Never hand the stripped form back to the file APIs.
    """
    text = str(path)
    if text.startswith(_UNC_PREFIX):
        return "\\\\" + text[len(_UNC_PREFIX) :]
    if text.startswith(_PREFIX):
        return text[len(_PREFIX) :]
    return text


def long_path_obj(path: str | os.PathLike[str], *, force: bool = False) -> Path:
    """Same as :func:`long_path` but returns a :class:`~pathlib.Path`."""
    return Path(long_path(path, force=force))


def safe_open(
    path: str | os.PathLike[str],
    mode: str = "rb",
    **kwargs: Any,
) -> IO[Any]:
    """Open a file, tolerating paths longer than MAX_PATH.

    Use this instead of the builtin :func:`open` anywhere the path can come
    from a user-chosen drawing folder.
    """
    return open(long_path(path), mode, **kwargs)


def exists(path: str | os.PathLike[str]) -> bool:
    """Long-path-safe existence check."""
    return os.path.exists(long_path(path))


def walk(root: str | os.PathLike[str]) -> Any:
    """Long-path-safe :func:`os.walk`, yielding prefixed directory paths."""
    return os.walk(long_path(root, force=is_windows()))
