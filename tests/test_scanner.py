"""Folder scanning, junk exclusion, and content hashing."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from engine.ingest.file_hasher import content_hash, find_duplicate_groups, quick_hash
from engine.ingest.folder_scanner import ScanOptions, scan_folder
from engine.utils.errors import NotFoundError, ValidationError
from tests.fixture_builder import SheetSpec, build_messy_folder, build_pdf


@pytest.fixture(scope="module")
def messy_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_messy_folder(tmp_path_factory.mktemp("messy"))


# ── Walking ────────────────────────────────────────────────────────────


def test_finds_drawings_in_subfolders(messy_folder: Path):
    result = scan_folder(messy_folder)
    names = {item.filename for item in result.drawings}

    assert "A-101-Rev-C.pdf" in names
    assert "A-102-Rev-C.pdf" in names  # one level down
    assert "A-103-Rev-C.pdf" in names  # two levels down


def test_junk_files_are_excluded_and_reported(messy_folder: Path):
    result = scan_folder(messy_folder)
    all_names = {item.filename for item in result.drawings + result.other_files}

    assert "Thumbs.db" not in all_names
    assert "desktop.ini" not in all_names
    assert "~$register.xlsx" not in all_names

    skipped = {Path(entry.path).name for entry in result.skipped}
    assert {"Thumbs.db", "desktop.ini", "~$register.xlsx"} <= skipped


def test_superseded_and_archive_folders_are_skipped_whole(messy_folder: Path):
    result = scan_folder(messy_folder)
    paths = " ".join(item.abs_path.lower() for item in result.drawings)

    assert "superseded" not in paths
    assert "_archive" not in paths

    reasons = {entry.reason for entry in result.skipped}
    assert any("superseded" in reason for reason in reasons)


def test_non_drawings_are_reported_separately(messy_folder: Path):
    result = scan_folder(messy_folder)
    others = {item.extension for item in result.other_files}

    assert ".dwg" in others  # the user must know DWGs exist
    assert ".xlsx" in others
    assert all(item.extension == ".pdf" for item in result.drawings)

    counts = result.other_by_extension()
    assert counts[".dwg"] == 1


def test_relative_paths_are_relative_to_the_root(messy_folder: Path):
    result = scan_folder(messy_folder)
    by_name = {item.filename: item for item in result.drawings}

    assert by_name["A-101-Rev-C.pdf"].rel_path == "A-101-Rev-C.pdf"
    assert by_name["A-102-Rev-C.pdf"].rel_path == str(Path("Architectural/A-102-Rev-C.pdf"))
    assert by_name["A-103-Rev-C.pdf"].depth == 2


def test_the_ignore_list_is_configurable(messy_folder: Path):
    keep_everything = ScanOptions(ignore_dirs=frozenset())
    result = scan_folder(messy_folder, keep_everything)

    assert any("superseded" in item.abs_path.lower() for item in result.drawings)


def test_results_are_sorted_for_a_stable_display(messy_folder: Path):
    result = scan_folder(messy_folder)
    paths = [item.rel_path.lower() for item in result.drawings]
    assert paths == sorted(paths)


# ── Failure modes ──────────────────────────────────────────────────────


def test_a_missing_folder_says_what_to_do(tmp_path: Path):
    with pytest.raises(NotFoundError) as info:
        scan_folder(tmp_path / "not-here")

    assert "could not be found" in info.value.message
    assert "network drive" in info.value.message  # the usual real cause


def test_pointing_at_a_file_is_explained(tmp_path: Path):
    target = build_pdf(tmp_path / "A-101.pdf")
    with pytest.raises(ValidationError) as info:
        scan_folder(target)

    assert "folder that holds the drawings" in info.value.message


def test_an_empty_folder_scans_without_error(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = scan_folder(empty)

    assert result.drawing_count == 0
    assert result.errors == []


# ── Performance ────────────────────────────────────────────────────────


def test_fast_pass_meets_the_two_second_target(tmp_path: Path):
    """Target from the plan: under 2 s for 300 files on a local drive."""
    root = tmp_path / "big"
    root.mkdir()
    payload = build_pdf(root / "seed.pdf").read_bytes()
    for index in range(300):
        (root / f"A-{index:04d}.pdf").write_bytes(payload)

    started = time.perf_counter()
    result = scan_folder(root)
    elapsed = time.perf_counter() - started

    assert result.drawing_count == 301
    assert elapsed < 2.0, f"fast pass took {elapsed:.2f}s for 301 files"


# ── Hashing ────────────────────────────────────────────────────────────


def test_identical_files_hash_the_same(tmp_path: Path):
    first = build_pdf(tmp_path / "a.pdf")
    second = tmp_path / "b.pdf"
    second.write_bytes(first.read_bytes())

    assert content_hash(first) == content_hash(second)
    assert quick_hash(first) == quick_hash(second)


def test_different_content_hashes_differently(tmp_path: Path):
    first = build_pdf(tmp_path / "a.pdf", [SheetSpec(drawing_no="A-101")])
    second = build_pdf(tmp_path / "b.pdf", [SheetSpec(drawing_no="A-102")])

    assert content_hash(first) != content_hash(second)


def test_quick_hash_separates_files_of_different_size(tmp_path: Path):
    small = tmp_path / "small.bin"
    large = tmp_path / "large.bin"
    small.write_bytes(b"x" * 1000)
    large.write_bytes(b"x" * 2000)

    assert quick_hash(small) != quick_hash(large)


def test_quick_hash_reads_both_ends_of_a_large_file(tmp_path: Path):
    """A change in the middle is allowed to slip past; a change at either end is not."""
    size = 500_000
    base = bytearray(b"a" * size)

    first = tmp_path / "first.bin"
    first.write_bytes(bytes(base))

    changed_start = bytearray(base)
    changed_start[0:10] = b"b" * 10
    second = tmp_path / "second.bin"
    second.write_bytes(bytes(changed_start))

    changed_end = bytearray(base)
    changed_end[-10:] = b"b" * 10
    third = tmp_path / "third.bin"
    third.write_bytes(bytes(changed_end))

    assert quick_hash(first) != quick_hash(second)
    assert quick_hash(first) != quick_hash(third)


def test_find_duplicate_groups_finds_the_copied_file(messy_folder: Path):
    result = scan_folder(messy_folder)
    groups = find_duplicate_groups([item.abs_path for item in result.drawings])

    assert len(groups) == 1
    names = {Path(path).name for group in groups.values() for path in group}
    assert names == {"A-101-Rev-C.pdf", "Copy of A-101-Rev-C.pdf"}


def test_an_unreadable_file_raises_a_typed_error(tmp_path: Path):
    from engine.utils.errors import UnreadableFileError

    with pytest.raises(UnreadableFileError):
        content_hash(tmp_path / "does-not-exist.pdf")
