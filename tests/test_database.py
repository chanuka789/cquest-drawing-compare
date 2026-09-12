"""Database creation, schema version, and basic inserts."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from engine.core.enums import PairStatus, Side
from engine.storage.db import (
    SCHEMA_VERSION_KEY,
    create_project_db,
    open_project_db,
    read_schema_version,
    session_scope,
)
from engine.storage.schema import SCHEMA_VERSION, File, Meta, Pair, Project, Sheet
from engine.utils.errors import NotFoundError, SchemaVersionError


def test_create_project_db_writes_schema_version(tmp_path):
    db_path = tmp_path / "demo.cqdc"
    engine = create_project_db(db_path)

    assert db_path.exists()
    assert read_schema_version(engine) == SCHEMA_VERSION

    with session_scope(engine) as session:
        row = session.scalar(select(Meta).where(Meta.key == SCHEMA_VERSION_KEY))
        assert row is not None
        assert row.value == str(SCHEMA_VERSION)

    engine.dispose()


def test_create_refuses_to_overwrite(tmp_path):
    db_path = tmp_path / "demo.cqdc"
    create_project_db(db_path).dispose()

    with pytest.raises(FileExistsError):
        create_project_db(db_path)

    create_project_db(db_path, overwrite=True).dispose()


def test_insert_project_file_and_sheet(tmp_path):
    engine = create_project_db(tmp_path / "demo.cqdc")

    with session_scope(engine) as session:
        project = Project(
            name="UVU Jeddah Tower - IFC Rev C to Rev D",
            old_folder=r"D:\GitHub\_cqdc-fixtures\old",
            new_folder=r"D:\GitHub\_cqdc-fixtures\new",
        )
        session.add(project)
        session.flush()

        drawing = File(
            project_id=project.id,
            side=Side.NEW,
            abs_path=r"D:\GitHub\_cqdc-fixtures\new\A-101.pdf",
            rel_path="A-101.pdf",
            filename="A-101.pdf",
            size=1024,
            page_count=1,
        )
        session.add(drawing)
        session.flush()

        session.add(Sheet(file_id=drawing.id, page_index=0, dwg_number="A-101", revision="D"))

    with session_scope(engine) as session:
        project = session.scalar(select(Project))
        assert project is not None
        assert project.created.tzinfo is not None

        sheet = session.scalar(select(Sheet))
        assert sheet is not None
        assert sheet.dwg_number == "A-101"

        stored_file = session.scalar(select(File))
        assert stored_file is not None
        assert stored_file.side == Side.NEW
        assert stored_file.is_readable is True

    engine.dispose()


def test_pair_status_defaults_to_ambiguous(tmp_path):
    engine = create_project_db(tmp_path / "demo.cqdc")

    with session_scope(engine) as session:
        project = Project(name="Pair defaults")
        session.add(project)
        session.flush()
        session.add(Pair(project_id=project.id))

    with session_scope(engine) as session:
        pair = session.scalar(select(Pair))
        assert pair is not None
        assert pair.status == PairStatus.AMBIGUOUS

    engine.dispose()


def test_foreign_keys_are_enforced(tmp_path):
    from sqlalchemy.exc import IntegrityError

    engine = create_project_db(tmp_path / "demo.cqdc")

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            File(
                project_id=999,  # no such project
                side=Side.OLD,
                abs_path="x",
                rel_path="x",
                filename="x",
            )
        )

    engine.dispose()


def test_open_missing_database_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        open_project_db(tmp_path / "nothing-here.cqdc")


def test_open_rejects_a_newer_schema(tmp_path):
    db_path = tmp_path / "future.cqdc"
    engine = create_project_db(db_path)

    with session_scope(engine) as session:
        row = session.scalar(select(Meta).where(Meta.key == SCHEMA_VERSION_KEY))
        assert row is not None
        row.value = str(SCHEMA_VERSION + 1)

    engine.dispose()

    with pytest.raises(SchemaVersionError):
        open_project_db(db_path)


def test_open_existing_database_succeeds(tmp_path):
    db_path = tmp_path / "demo.cqdc"
    create_project_db(db_path).dispose()

    engine = open_project_db(db_path)
    assert read_schema_version(engine) == SCHEMA_VERSION
    engine.dispose()


def test_an_older_schema_is_migrated_on_open(monkeypatch, tmp_path):
    """A schema-1 file (from a build before Phase 3) opens cleanly, running
    every migration in turn: rename_log rebuilt at 2, comparison storage at 3."""
    import engine.storage.db as db_module

    db_path = tmp_path / "legacy.cqdc"

    # Write a file as the old build would have: schema version 1.
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 1)
    old_engine = db_module.create_project_db(db_path)
    with db_module.session_scope(old_engine) as session:
        session.execute(db_module.text("DROP TABLE IF EXISTS rename_log"))
        session.execute(
            db_module.text(
                "CREATE TABLE rename_log ("
                " id INTEGER PRIMARY KEY,"
                " project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,"
                " old_name TEXT NOT NULL,"
                " new_name TEXT NOT NULL,"
                " applied_at DATETIME,"
                " reversed_at DATETIME)"
            )
        )
    old_engine.dispose()
    monkeypatch.undo()  # restore the current SCHEMA_VERSION before migrating

    engine = open_project_db(db_path)
    assert read_schema_version(engine) == SCHEMA_VERSION
    with session_scope(engine) as session:
        columns = {
            row[1] for row in session.execute(db_module.text("PRAGMA table_info(rename_log)"))
        }
        change_columns = {
            row[1] for row in session.execute(db_module.text("PRAGMA table_info(change)"))
        }
        tables = {
            row[0]
            for row in session.execute(
                db_module.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "source_path" in columns
    assert "source_hash" in columns
    # Schema 3: the Phase 5 comparison storage.
    assert {"kind", "streams", "confidence", "detail_json"} <= change_columns
    assert {"change_hatch", "comparison_run", "filtered_change"} <= tables
    engine.dispose()
