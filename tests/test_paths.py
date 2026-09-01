"""App data paths: resolution, creation, and isolation from the repository."""

from __future__ import annotations

from pathlib import Path

from engine.storage.paths import (
    APP_DATA_ENV_VAR,
    AppPaths,
    bundle_root,
    is_frozen,
    resolve_paths,
)


def test_resolve_paths_builds_the_expected_tree(tmp_path: Path):
    paths = resolve_paths(tmp_path / "CQuest")

    assert paths.root == tmp_path / "CQuest"
    assert paths.db == paths.root / "db"
    assert paths.logs == paths.root / "logs"
    assert paths.cache == paths.root / "cache"
    assert paths.profiles == paths.root / "profiles"


def test_ensure_creates_every_directory(tmp_path: Path):
    paths = resolve_paths(tmp_path / "CQuest").ensure()

    for directory in (paths.root, paths.db, paths.logs, paths.cache, paths.profiles):
        assert directory.is_dir()


def test_ensure_is_repeatable(tmp_path: Path):
    root = tmp_path / "CQuest"
    resolve_paths(root).ensure()
    resolve_paths(root).ensure()  # must not raise
    assert root.is_dir()


def test_project_db_names_one_file_per_project(tmp_path: Path):
    paths = resolve_paths(tmp_path).ensure()
    assert paths.project_db("uvu-rev-c-to-d") == paths.db / "uvu-rev-c-to-d.cqdc"


def test_as_dict_is_all_strings(tmp_path: Path):
    values = resolve_paths(tmp_path).as_dict()

    assert set(values) == {"root", "db", "logs", "cache", "profiles"}
    assert all(isinstance(value, str) for value in values.values())


def test_env_override_redirects_app_data(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(APP_DATA_ENV_VAR, str(tmp_path / "elsewhere"))
    assert resolve_paths().root == tmp_path / "elsewhere"


def test_app_data_never_lives_inside_the_repository(app_data_dir: Path):
    """The session fixture redirects app data. Prove it is not in the repo."""
    repo = Path(__file__).resolve().parents[1]
    assert not app_data_dir.is_relative_to(repo)


def test_app_paths_is_immutable(tmp_path: Path):
    import dataclasses

    import pytest

    paths: AppPaths = resolve_paths(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.root = tmp_path  # type: ignore[misc]


def test_bundle_root_points_at_the_repository_in_development():
    assert is_frozen() is False
    assert (bundle_root() / "engine").is_dir()
