"""The output workspace: validation, structure, and the audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from engine.core.workspace import (
    WORKSPACE_FOLDERS,
    create_workspace,
    is_inside,
    suggest_output_folder,
    unique_path,
    validate_output_folder,
    write_audit_log,
)

# ── The rule: never write inside an input folder ───────────────────────


def test_an_output_folder_inside_an_input_folder_is_refused(tmp_path: Path):
    """Otherwise the next scan reads the register as a drawing."""
    new_folder = tmp_path / "IFC Rev D"
    new_folder.mkdir()
    output = new_folder / "Comparison"

    result = validate_output_folder(output, tmp_path / "IFC Rev C", new_folder)

    assert not result.is_valid
    assert result.is_inside_input
    assert "inside the current issue folder" in result.errors[0]
    assert "outside it" in result.errors[0]  # says what to do


def test_the_output_folder_cannot_be_an_input_folder(tmp_path: Path):
    old_folder = tmp_path / "old"
    old_folder.mkdir()

    result = validate_output_folder(old_folder, old_folder, tmp_path / "new")

    assert not result.is_valid
    assert result.is_same_as_input


def test_a_folder_beside_the_inputs_is_fine(tmp_path: Path):
    old_folder = tmp_path / "old"
    new_folder = tmp_path / "new"
    output = tmp_path / "Comparisons"
    for folder in (old_folder, new_folder, output):
        folder.mkdir()

    result = validate_output_folder(output, old_folder, new_folder)

    assert result.is_valid
    assert result.is_writable
    assert not result.is_inside_input


def test_is_inside():
    assert is_inside(r"D:\a\b\c", r"D:\a")
    assert is_inside(r"D:\a", r"D:\a")  # the same folder counts
    assert not is_inside(r"D:\ab", r"D:\a")  # not a path prefix trick
    assert not is_inside(r"D:\b", r"D:\a")


# ── Write access, checked up front ─────────────────────────────────────


def test_a_writable_folder_passes(tmp_path: Path):
    result = validate_output_folder(tmp_path)

    assert result.is_writable
    assert result.is_valid
    assert result.free_bytes > 0


def test_a_folder_that_does_not_exist_yet_is_allowed_with_a_note(tmp_path: Path):
    result = validate_output_folder(tmp_path / "not-made-yet")

    assert result.is_valid
    assert not result.exists
    assert any("will be created" in warning for warning in result.warnings)


def test_a_location_that_does_not_exist_is_refused(tmp_path: Path):
    result = validate_output_folder(tmp_path / "no" / "such" / "place")

    assert not result.is_valid
    assert "network drive" in result.errors[0]


def test_a_non_empty_folder_warns_but_does_not_block(tmp_path: Path):
    (tmp_path / "something.txt").write_text("hello", encoding="utf-8")

    result = validate_output_folder(tmp_path)

    assert result.is_valid  # a warning, not an error
    assert not result.is_empty
    assert any("not empty" in warning for warning in result.warnings)


# ── Suggesting a name ──────────────────────────────────────────────────


def test_the_suggested_name_says_what_it_contains(tmp_path: Path):
    suggestion = suggest_output_folder(
        tmp_path / "03_IFC_RevC",
        tmp_path / "03_IFC_RevD",
        "C",
        "D",
        today=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert suggestion.name == "Compare_RevC_to_RevD_2026-09-01"
    assert suggestion.parent.name == "Comparisons"


def test_the_suggestion_falls_back_to_folder_names(tmp_path: Path):
    suggestion = suggest_output_folder(
        tmp_path / "Issue A",
        tmp_path / "Issue B",
        None,
        None,
        today=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert "Issue-A" in suggestion.name
    assert "Issue-B" in suggestion.name


def test_the_suggestion_is_never_inside_an_input_folder(tmp_path: Path):
    old_folder = tmp_path / "old"
    new_folder = tmp_path / "new"
    suggestion = suggest_output_folder(old_folder, new_folder, "C", "D")

    assert not is_inside(suggestion, old_folder)
    assert not is_inside(suggestion, new_folder)


# ── Creating it ────────────────────────────────────────────────────────


def test_the_workspace_structure_is_created(tmp_path: Path):
    workspace = create_workspace(tmp_path / "Compare_RevC_to_RevD")

    for name in WORKSPACE_FOLDERS:
        assert (workspace.root / name).is_dir()

    assert workspace.register_dir.name == "01_Register"
    assert workspace.scan_cache_path.parent == workspace.audit_dir


def test_creating_a_workspace_twice_is_harmless(tmp_path: Path):
    create_workspace(tmp_path / "ws")
    workspace = create_workspace(tmp_path / "ws")
    assert workspace.root.is_dir()


# ── Never overwrite ────────────────────────────────────────────────────


def test_an_existing_register_is_never_overwritten(tmp_path: Path):
    """Someone may already have sent the first one to the design team."""
    first = tmp_path / "Drawing_Register.xlsx"
    first.write_bytes(b"original")

    second = unique_path(first)
    assert second.name == "Drawing_Register (2).xlsx"

    second.write_bytes(b"second")
    assert unique_path(first).name == "Drawing_Register (3).xlsx"
    assert first.read_bytes() == b"original"


def test_a_free_path_is_returned_unchanged(tmp_path: Path):
    target = tmp_path / "Drawing_Register.xlsx"
    assert unique_path(target) == target


# ── The audit log ──────────────────────────────────────────────────────


def test_the_audit_log_records_what_was_run(tmp_path: Path):
    """This is what makes the output defensible in a variation claim."""
    workspace = create_workspace(tmp_path / "ws")

    path = write_audit_log(
        workspace,
        {
            "old_folder": r"D:\old",
            "new_folder": r"D:\new",
            "issue_type": "partial",
            "counts": {"revised": 12, "not_reissued": 188},
            "profile": "keo",
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["app"] == "C-Quest Drawing Compare"
    assert payload["version"]
    assert payload["written_at"]
    assert payload["issue_type"] == "partial"
    assert payload["counts"]["revised"] == 12
    assert payload["profile"] == "keo"
