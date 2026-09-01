"""Windows long path handling.

Real drawing folders exceed MAX_PATH routinely, so this helper is load-bearing
for every stage that touches a user-chosen folder.
"""

from __future__ import annotations

import sys

import pytest

from engine.utils import longpath
from engine.utils.longpath import (
    LONG_PATH_THRESHOLD,
    has_prefix,
    is_windows,
    long_path,
    safe_open,
    strip_prefix,
)

B = "\\"
LONG_DRIVE_PATH = "D:" + B + "x" + (B + "verylongfoldername") * 20 + B + "sheet.pdf"
LONG_UNC_PATH = B + B + "srv" + B + "share" + (B + "deepfolder") * 30 + B + "sheet.pdf"

windows_only = pytest.mark.skipif(not is_windows(), reason="Windows path semantics")


def test_the_test_paths_really_are_long():
    assert len(LONG_DRIVE_PATH) > LONG_PATH_THRESHOLD
    assert len(LONG_UNC_PATH) > LONG_PATH_THRESHOLD


@windows_only
def test_a_short_path_is_left_alone(tmp_path):
    result = long_path(tmp_path / "A-101.pdf")
    assert not has_prefix(result)
    assert result.endswith("A-101.pdf")


@windows_only
def test_a_long_drive_path_gets_the_prefix():
    result = long_path(LONG_DRIVE_PATH)
    assert result.startswith("\\\\?\\D:")
    assert not result.startswith("\\\\?\\UNC")


@windows_only
def test_a_long_unc_path_gets_the_unc_prefix():
    result = long_path(LONG_UNC_PATH)
    assert result.startswith("\\\\?\\UNC\\srv\\share")


@windows_only
def test_force_prefixes_even_a_short_path(tmp_path):
    assert has_prefix(long_path(tmp_path, force=True))


@windows_only
def test_prefixing_is_not_applied_twice():
    once = long_path(LONG_DRIVE_PATH)
    assert long_path(once) == once


@windows_only
@pytest.mark.parametrize("path", [LONG_DRIVE_PATH, LONG_UNC_PATH])
def test_strip_prefix_round_trips(path):
    assert strip_prefix(long_path(path)) == path


def test_strip_prefix_leaves_an_unprefixed_path_alone():
    assert strip_prefix("D:\\drawings\\A-101.pdf") == "D:\\drawings\\A-101.pdf"


@windows_only
def test_relative_segments_are_normalised_away():
    result = long_path("D:" + B + "a" + B + ".." + B + "b" + B + "c.pdf", force=True)
    assert ".." not in result
    assert result.endswith("b\\c.pdf")


def test_safe_open_reads_a_real_file(tmp_path):
    target = tmp_path / "A-101.txt"
    target.write_text("drawing", encoding="utf-8")

    with safe_open(target, "r", encoding="utf-8") as handle:
        assert handle.read() == "drawing"


def test_exists_handles_both_cases(tmp_path):
    target = tmp_path / "A-101.txt"
    assert longpath.exists(target) is False
    target.write_text("x", encoding="utf-8")
    assert longpath.exists(target) is True


def test_is_windows_matches_the_platform():
    assert is_windows() is (sys.platform == "win32")


@windows_only
def test_a_genuinely_long_path_can_be_written_and_read_back(tmp_path):
    """The point of the whole module: get past MAX_PATH on a real disk."""
    deep = tmp_path
    for index in range(12):
        deep = deep / f"folder-{index:02d}-{'x' * 18}"

    target = deep / "UVU-KEO-ARC-L03-DR-A-001234-Rev-D-Reflected-Ceiling-Plan.pdf"
    assert len(str(target)) > LONG_PATH_THRESHOLD

    import os

    os.makedirs(long_path(deep, force=True), exist_ok=True)
    with safe_open(target, "w", encoding="utf-8") as handle:
        handle.write("sheet")

    assert longpath.exists(target)
    with safe_open(target, "r", encoding="utf-8") as handle:
        assert handle.read() == "sheet"
