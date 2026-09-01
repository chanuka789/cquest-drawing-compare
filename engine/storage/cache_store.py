"""Scan cache: never inspect the same unchanged file twice.

A deep pass over 300 A0 drawings on a network share takes minutes. Nothing
about a file changes unless the file changes, so results are keyed by
`(absolute path, size, modified time)`. If all three match, the deep pass is
skipped entirely and the second scan of a folder is effectively instant.

The cache lives in the comparison workspace (`_audit/scan_cache.db`) so it
travels with the job rather than polluting a global store. A separate SQLite
file is used rather than the project database because the cache is disposable:
deleting it costs time, never data.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

CACHE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection (
    abs_path   TEXT    NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL    NOT NULL,
    kind       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    PRIMARY KEY (abs_path, size, mtime, kind)
);
"""


@dataclass(frozen=True, slots=True)
class CacheKey:
    """What makes a cached result still valid."""

    abs_path: str
    size: int
    mtime: float

    @classmethod
    def for_file(cls, path: str | Path) -> CacheKey:
        from engine.utils.longpath import long_path

        stat = Path(long_path(path)).stat()
        return cls(abs_path=str(path), size=stat.st_size, mtime=stat.st_mtime)


class CacheStore:
    """SQLite-backed cache of expensive per-file results.

    Safe to use from several threads: SQLite connections are per-thread and a
    lock guards writes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._initialise()

    # -- connection ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.path), timeout=5.0)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = connection
        return connection

    def _initialise(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(CACHE_SCHEMA_VERSION)),
            )
            connection.commit()

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    # -- reading and writing -------------------------------------------

    def get(self, key: CacheKey, kind: str) -> dict[str, Any] | None:
        """Return the cached payload, or None when the file has changed."""
        row = (
            self._connect()
            .execute(
                "SELECT payload FROM inspection WHERE abs_path=? AND size=? AND mtime=? AND kind=?",
                (key.abs_path, key.size, key.mtime, kind),
            )
            .fetchone()
        )

        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None  # a corrupt cache row is just a miss

    def put(self, key: CacheKey, kind: str, payload: dict[str, Any]) -> None:
        import time

        with self._lock:
            connection = self._connect()
            connection.execute(
                "INSERT OR REPLACE INTO inspection "
                "(abs_path, size, mtime, kind, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key.abs_path, key.size, key.mtime, kind, json.dumps(payload), time.time()),
            )
            connection.commit()

    def put_many(self, items: list[tuple[CacheKey, str, dict[str, Any]]]) -> None:
        """Write a batch in one transaction. Much faster for a whole scan."""
        import time

        now = time.time()
        with self._lock:
            connection = self._connect()
            connection.executemany(
                "INSERT OR REPLACE INTO inspection "
                "(abs_path, size, mtime, kind, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (key.abs_path, key.size, key.mtime, kind, json.dumps(payload), now)
                    for key, kind, payload in items
                ],
            )
            connection.commit()

    # -- housekeeping --------------------------------------------------

    def prune_missing(self, known_paths: set[str]) -> int:
        """Drop rows for files that no longer exist in the scanned set."""
        with self._lock:
            connection = self._connect()
            rows = connection.execute("SELECT DISTINCT abs_path FROM inspection").fetchall()
            stale = [(row[0],) for row in rows if row[0] not in known_paths]
            if stale:
                connection.executemany("DELETE FROM inspection WHERE abs_path=?", stale)
                connection.commit()
        if stale:
            logger.debug("Pruned {} stale cache rows", len(stale))
        return len(stale)

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self._connect().execute("SELECT COUNT(*) FROM inspection").fetchone()
        else:
            row = (
                self._connect()
                .execute("SELECT COUNT(*) FROM inspection WHERE kind=?", (kind,))
                .fetchone()
            )
        return int(row[0])

    def clear(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.execute("DELETE FROM inspection")
            connection.commit()


@contextmanager
def open_cache(path: str | Path) -> Iterator[CacheStore]:
    """Open a cache and close it again, even if the run fails."""
    store = CacheStore(path)
    try:
        yield store
    finally:
        store.close()
