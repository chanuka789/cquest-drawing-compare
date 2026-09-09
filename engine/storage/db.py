"""Database engine and session handling.

One SQLite file per project, opened with foreign keys enforced and WAL
journalling so a reader is never blocked by the writer during a long run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from engine.storage.schema import SCHEMA_VERSION, Base, Meta, RenameLog
from engine.utils.errors import NotFoundError, SchemaVersionError

#: Key used in the `meta` table.
SCHEMA_VERSION_KEY = "schema_version"


def _migrate_1_to_2(engine: Engine) -> None:
    """1 -> 2: RenameLog was scaffolding (no rows were ever written) but its
    columns could not record source paths or hashes. Rebuild it with the full
    audit shape."""
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS rename_log"))
    RenameLog.__table__.create(engine, checkfirst=True)
    logger.info("Migrated project database to schema 2 (rename_log rebuilt)")


#: Version-to-version migration steps. Each entry upgrades a file at that
#: version to the next one. Fresh files are created at SCHEMA_VERSION, so only
#: files written by an older build ever run a migration.
MIGRATIONS: dict[int, Callable[[Engine], None]] = {1: _migrate_1_to_2}


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Apply the per-connection PRAGMAs every project database needs."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _make_engine(path: Path) -> Engine:
    # future=True is the default in SQLAlchemy 2.0; stated for clarity.
    return create_engine(f"sqlite:///{path}", echo=False, future=True)


def create_project_db(path: str | Path, *, overwrite: bool = False) -> Engine:
    """Create a new project database at *path* and return its engine.

    Raises :class:`FileExistsError` if the file exists and *overwrite* is False.
    """
    target = Path(path)
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"A project database already exists at {target}")
        target.unlink()

    target.parent.mkdir(parents=True, exist_ok=True)
    engine = _make_engine(target)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Meta(key=SCHEMA_VERSION_KEY, value=str(SCHEMA_VERSION)))
        session.commit()

    logger.info("Created project database | path={} | schema_version={}", target, SCHEMA_VERSION)
    return engine


def _run_migrations(engine: Engine, start_version: int) -> None:
    """Upgrade an older file step by step, then record the new version.

    Migrations are strictly forward and small; each entry upgrades exactly one
    version. A file newer than this build is refused before this runs.
    """
    version = start_version
    with Session(engine) as session:
        while version < SCHEMA_VERSION:
            handler = MIGRATIONS.get(version)
            if handler is None:
                raise SchemaVersionError(
                    f"The database uses schema {version} and no migration path to "
                    f"{SCHEMA_VERSION} exists."
                )
            handler(engine)
            version += 1
            row = session.scalar(select(Meta).where(Meta.key == SCHEMA_VERSION_KEY))
            if row is None:
                session.add(Meta(key=SCHEMA_VERSION_KEY, value=str(version)))
            else:
                row.value = str(version)
            session.commit()


def open_project_db(path: str | Path) -> Engine:
    """Open an existing project database, migrating it if it is older.

    A file written by a newer version of the application is refused — never
    downgrade a file a newer build may have touched.
    """
    target = Path(path)
    if not target.exists():
        raise NotFoundError(f"No project database at {target}")

    engine = _make_engine(target)
    version = read_schema_version(engine)

    if version is None:
        raise SchemaVersionError(f"{target.name} has no schema version and cannot be opened")
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{target.name} was written by a newer version of the application "
            f"(file schema {version}, this build understands {SCHEMA_VERSION}). "
            "Update the application to open it."
        )
    if version < SCHEMA_VERSION:
        # No migrations existed before; the first one arrives with schema 2.
        _run_migrations(engine, version)
        version = read_schema_version(engine)

    logger.info("Opened project database | path={} | schema_version={}", target, version)
    return engine


def read_schema_version(engine: Engine) -> int | None:
    """Return the schema version stored in the file, or None if absent."""
    with Session(engine) as session:
        row = session.scalar(select(Meta).where(Meta.key == SCHEMA_VERSION_KEY))
        if row is None:
            return None
        try:
            return int(row.value)
        except ValueError:
            return None


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to *engine*."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def close_engine(engine: Engine) -> None:
    """Dispose of the engine so the SQLite file can be moved or deleted."""
    engine.dispose()
