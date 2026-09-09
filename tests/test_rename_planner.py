"""Planner tests: clean plans, unchanged rows, collisions, Windows traps."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.core.models import SheetRecord
from engine.naming import rename_planner
from engine.naming.rename_planner import ActionStatus, RenameMode, build_plan

CONTEXT = {"project": "UVU", "originator": "KEO"}
TEMPLATE = "{project}-{originator}-{number}"


def sheet(path: Path, *, drawing_no: str | None = None, filename: str | None = None) -> SheetRecord:
    return SheetRecord(
        abs_path=str(path),
        filename=filename or path.name,
        drawing_no=drawing_no,
    )


def _make(source: Path, name: str, content: bytes = b"drawing") -> Path:
    target = source / name
    target.write_bytes(content)
    return target


# ── A clean copy plan ──────────────────────────────────────────────────


def test_copy_plan_builds_clean_targets(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"

    files = [
        sheet(_make(source, "A-101.pdf"), drawing_no="A-101"),
        sheet(_make(source, "A-102.pdf"), drawing_no="A-102"),
        sheet(_make(source, "A-103.pdf"), drawing_no="A-103"),
    ]
    plan = build_plan(files, TEMPLATE, CONTEXT, out)

    assert plan.can_apply
    assert plan.mode is RenameMode.COPY
    assert len(plan.actions) == 3
    assert all(action.status is ActionStatus.OK for action in plan.actions)
    assert {Path(a.target_path) for a in plan.actions} == {
        out / "UVU-KEO-101.pdf",
        out / "UVU-KEO-102.pdf",
        out / "UVU-KEO-103.pdf",
    }
    assert plan.summary["to_change"] == 3
    assert plan.summary["unchanged"] == 0
    assert plan.summary["problems"] == 0
    assert plan.warnings == []


def test_non_drawing_files_are_skipped_with_a_warning(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    pdf = sheet(_make(source, "A-101.pdf"), drawing_no="A-101")
    note = sheet(_make(source, "notes.txt"))
    readme = sheet(_make(source, "README.md"))

    plan = build_plan([pdf, note, readme], TEMPLATE, CONTEXT, tmp_path / "out")

    assert len(plan.actions) == 1
    assert len(plan.warnings) == 2
    assert "notes.txt" in plan.warnings[0]
    assert plan.can_apply


# ── Unchanged and case-only rows ───────────────────────────────────────


def test_unchanged_when_target_equals_source(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    original = _make(source, "A-101.pdf")
    plan = build_plan(
        [sheet(original, drawing_no="A-101")],
        "{original}_{rev}",
        CONTEXT,
        tmp_path / "out",
        mode=RenameMode.IN_PLACE,
    )

    action = plan.actions[0]
    assert action.status is ActionStatus.UNCHANGED
    assert action.target_path == str(original)
    assert plan.summary["unchanged"] == 1
    assert plan.summary["problems"] == 0
    assert plan.can_apply


def test_case_only_rename_is_ok_with_a_warning(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    original = _make(source, "abc.pdf")
    plan = build_plan(
        [sheet(original, drawing_no=None)],
        "{number}",
        {"number": "ABC"},
        tmp_path / "out",
        mode=RenameMode.IN_PLACE,
    )

    action = plan.actions[0]
    assert action.status is ActionStatus.OK
    assert "case-only rename" in " ".join(action.warnings)
    assert Path(action.target_path).name == "ABC.pdf"
    assert plan.summary["to_change"] == 1


# ── Collisions ─────────────────────────────────────────────────────────


def test_two_rows_with_the_same_target_get_a_suffix(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"
    first = sheet(_make(source, "first.pdf"), drawing_no="A-101")
    second = sheet(_make(source, "second.pdf"), drawing_no="A-101")

    plan = build_plan([first, second], TEMPLATE, CONTEXT, out)

    assert len(plan.actions) == 2
    assert [a.status for a in plan.actions] == [ActionStatus.OK, ActionStatus.OK]
    first_target = Path(plan.actions[0].target_path)
    second_target = Path(plan.actions[1].target_path)
    assert first_target == out / "UVU-KEO-101.pdf"
    assert second_target == out / "UVU-KEO-101_2.pdf"
    assert second_target != first_target
    warning = " ".join(plan.actions[1].warnings)
    assert "two files would share the name" in warning
    assert "UVU-KEO-101_2.pdf" in warning
    assert plan.summary["to_change"] == 2
    assert plan.summary["problems"] == 0


def test_target_that_already_exists_on_disk_is_not_overwritten(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    (out / "UVU-KEO-101.pdf").write_bytes(b"existing")
    plan = build_plan(
        [sheet(_make(source, "A-101.pdf"), drawing_no="A-101")], TEMPLATE, CONTEXT, out
    )

    action = plan.actions[0]
    assert action.status is ActionStatus.OK
    assert Path(action.target_path) == out / "UVU-KEO-101_2.pdf"
    assert "already exists on disk; will not overwrite" in " ".join(action.warnings)
    assert (out / "UVU-KEO-101.pdf").read_bytes() == b"existing"


# ── Windows name traps ─────────────────────────────────────────────────


def test_reserved_device_name_is_marked(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    plan = build_plan(
        [sheet(_make(source, "drawing.pdf"))],
        "{number}",
        {"number": "CON"},
        tmp_path / "out",
    )

    action = plan.actions[0]
    assert action.status is ActionStatus.RESERVED_NAME
    assert not plan.can_apply
    assert "reserved Windows device name" in " ".join(action.warnings)
    assert plan.summary["problems"] == 1


def test_invalid_character_surviving_sanitise_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # sanitise_name already strips invalid characters inside render; make it a
    # pass-through so the planner's own double-check has something to catch.
    monkeypatch.setattr("engine.naming.template.sanitise_name", lambda stem: stem)
    source = tmp_path / "src"
    source.mkdir()

    plan = build_plan(
        [sheet(_make(source, "drawing.pdf"))],
        "{project}",
        {"project": "A<B"},
        tmp_path / "out",
    )

    action = plan.actions[0]
    assert action.status is ActionStatus.INVALID_NAME
    assert not plan.can_apply


def test_source_missing_is_marked(tmp_path: Path):
    missing = tmp_path / "src" / "gone.pdf"
    plan = build_plan([sheet(missing, drawing_no="A-101")], TEMPLATE, CONTEXT, tmp_path / "out")

    action = plan.actions[0]
    assert action.status is ActionStatus.SOURCE_MISSING
    assert "not found" in " ".join(action.warnings)


def test_locked_source_is_marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src"
    source.mkdir()
    plan_file = _make(source, "A-101.pdf")

    def deny_open(_path: object) -> None:
        raise PermissionError("simulated lock")

    monkeypatch.setattr(rename_planner, "_can_open", deny_open)
    plan = build_plan([sheet(plan_file, drawing_no="A-101")], TEMPLATE, CONTEXT, tmp_path / "out")

    action = plan.actions[0]
    assert action.status is ActionStatus.LOCKED
    assert "locked" in " ".join(action.warnings).lower()
    assert plan.can_apply  # locked rows block the file, not the whole plan


# ── Discipline folders ─────────────────────────────────────────────────


def test_discipline_folder_is_derived_from_role(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    plan = build_plan(
        [sheet(_make(source, "A-101.pdf"), drawing_no="UVU-KEO-XX-03-DR-A-0001")],
        "{project}-{originator}-{volume}-{level}-{type}-{role}-{number}",
        CONTEXT,
        tmp_path / "out",
        use_discipline_folders=True,
    )

    action = plan.actions[0]
    assert action.target_folder == "Architectural"
    assert Path(action.target_path).parent == tmp_path / "out" / "Architectural"
