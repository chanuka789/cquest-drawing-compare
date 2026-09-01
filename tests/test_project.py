"""The workspace database is created on first start and reopened after that."""

from __future__ import annotations

from engine.core.project import ensure_workspace_db, workspace_db_path
from engine.storage.db import read_schema_version
from engine.storage.schema import SCHEMA_VERSION


def test_workspace_db_lives_under_the_app_data_db_folder():
    path = workspace_db_path()
    assert path.parent.name == "db"
    assert path.suffix == ".cqdc"


def test_first_start_creates_it_and_the_next_start_reopens_it():
    path = workspace_db_path()
    if path.exists():
        path.unlink()

    engine = ensure_workspace_db()
    try:
        assert path.exists()
        assert read_schema_version(engine) == SCHEMA_VERSION
    finally:
        engine.dispose()

    reopened = ensure_workspace_db()
    try:
        assert read_schema_version(reopened) == SCHEMA_VERSION
    finally:
        reopened.dispose()


def test_starting_the_app_creates_the_database(client):
    """The lifespan handler must leave a real file on disk."""
    assert client.get("/api/health").status_code == 200
    assert workspace_db_path().exists()
