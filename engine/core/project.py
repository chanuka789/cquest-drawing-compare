"""Project lifecycle.

Phase 1 needs only one thing from this module: a real database file on the
real machine, so the storage layer is proven outside the test suite.

The product model is one SQLite file per comparison project. Until the Setup
screen can create one (Phase 2), the engine keeps a single `workspace`
database that holds the schema and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from sqlalchemy import Engine

from engine.storage.db import create_project_db, open_project_db
from engine.storage.paths import get_app_paths

WORKSPACE_SLUG = "workspace"


def workspace_db_path() -> Path:
    """Path of the workspace database under the app data folder."""
    return get_app_paths().project_db(WORKSPACE_SLUG)


def ensure_workspace_db() -> Engine:
    """Open the workspace database, creating it on first run."""
    path = workspace_db_path()

    if path.exists():
        return open_project_db(path)

    logger.info("No workspace database yet; creating one at {}", path)
    return create_project_db(path)
