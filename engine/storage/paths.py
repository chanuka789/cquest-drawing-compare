"""Where the application keeps its own data.

Everything the app writes lives under `%LOCALAPPDATA%\\CQuest\\DrawingCompare\\`,
never inside the repository and never next to the user's drawings.

This module deliberately does not use `__file__` to locate the app data
directory. Under PyInstaller `__file__` points into a temporary extraction
folder that is deleted when the process exits, so anything written there
would be lost.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_VENDOR = "CQuest"
APP_FOLDER = "DrawingCompare"

#: Set this environment variable to redirect all app data (used by the tests).
APP_DATA_ENV_VAR = "CQDC_APP_DATA"


def _local_app_data() -> Path:
    """Return the platform's per-user application data directory."""
    override = os.environ.get(APP_DATA_ENV_VAR)
    if override:
        return Path(override)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_VENDOR / APP_FOLDER
        return Path.home() / "AppData" / "Local" / APP_VENDOR / APP_FOLDER

    # Not a supported target, but keep development on other platforms possible.
    return Path.home() / ".local" / "share" / APP_VENDOR / APP_FOLDER


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved application data directories."""

    root: Path
    db: Path
    logs: Path
    cache: Path
    profiles: Path

    def ensure(self) -> AppPaths:
        """Create every directory if it does not already exist."""
        for directory in (self.root, self.db, self.logs, self.cache, self.profiles):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def project_db(self, project_slug: str) -> Path:
        """Path of the SQLite file for one project. One file per project."""
        return self.db / f"{project_slug}.cqdc"

    def as_dict(self) -> dict[str, str]:
        """Plain string mapping, safe to send over the API."""
        return {
            "root": str(self.root),
            "db": str(self.db),
            "logs": str(self.logs),
            "cache": str(self.cache),
            "profiles": str(self.profiles),
        }


def resolve_paths(root: Path | None = None) -> AppPaths:
    """Build an :class:`AppPaths` under *root*, defaulting to the app data folder."""
    base = root if root is not None else _local_app_data()
    return AppPaths(
        root=base,
        db=base / "db",
        logs=base / "logs",
        cache=base / "cache",
        profiles=base / "profiles",
    )


@lru_cache(maxsize=1)
def get_app_paths() -> AppPaths:
    """Return the process-wide app paths, created on first use."""
    return resolve_paths().ensure()


def bundle_root() -> Path:
    """Return the directory holding bundled read-only resources.

    Under PyInstaller this is the extraction folder (`sys._MEIPASS`); in
    development it is the repository root. Use it for shipped assets such as
    `ui/dist` and `profiles/`, never for data the app writes.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))
