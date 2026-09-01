"""Shared test fixtures.

The whole suite runs against a temporary app data directory. Nothing here is
allowed to touch the real `%LOCALAPPDATA%\\CQuest\\DrawingCompare` folder or
the developer's own logs and databases.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from engine.settings import get_settings
from engine.storage.paths import APP_DATA_ENV_VAR, AppPaths, get_app_paths, resolve_paths


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_app_paths.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def app_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Redirect all app data to a temporary directory for the whole session."""
    root = tmp_path_factory.mktemp("cqdc-appdata")

    previous = {
        APP_DATA_ENV_VAR: os.environ.get(APP_DATA_ENV_VAR),
        "CQDC_DEV": os.environ.get("CQDC_DEV"),
        "CQDC_LOG_LEVEL": os.environ.get("CQDC_LOG_LEVEL"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
    }

    os.environ[APP_DATA_ENV_VAR] = str(root)
    os.environ["CQDC_DEV"] = "0"
    os.environ["CQDC_LOG_LEVEL"] = "WARNING"
    # Tests must never behave differently because a real key happens to be set.
    os.environ["ANTHROPIC_API_KEY"] = ""
    _reset_caches()

    yield root

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _reset_caches()


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    """A fresh, isolated set of app directories for one test."""
    return resolve_paths(tmp_path / "appdata").ensure()


@pytest.fixture
def project_db(tmp_path: Path) -> Iterator[Engine]:
    """A temporary project database, disposed of when the test ends."""
    from engine.storage.db import create_project_db

    engine = create_project_db(tmp_path / "test-project.cqdc")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient over a freshly built application."""
    from engine.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
