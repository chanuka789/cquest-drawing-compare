"""Executor and undo-log tests: apply, refuse, cancel, reverse, verify."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.core.jobs import CancelToken
from engine.core.models import SheetRecord
from engine.ingest.file_hasher import content_hash
from engine.naming import rename_executor
from engine.naming.rename_executor import execute_plan
from engine.naming.rename_planner import (
    ActionStatus,
    RenameAction,
    RenameMode,
    RenamePlan,
    build_plan,
)
from engine.naming.undo_log import UndoLog

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


def _copy_plan(source: Path, out: Path, names: list[str]) -> RenamePlan:
    sheets = [sheet(_make(source, name), drawing_no=Path(name).stem) for name in names]
    return build_plan(sheets, TEMPLATE, CONTEXT, out)


# ── Copy mode: apply, verify, log ──────────────────────────────────────


def test_copy_mode_creates_files_verifies_hash_and_writes_log(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"
    plan = _copy_plan(source, out, ["A-101.pdf", "A-102.pdf"])
    log = tmp_path / "audit" / "rename_log.json"

    result = execute_plan(plan, log)

    assert result.completed == 2
    assert result.failed == []
    assert result.cancelled is False
    assert result.undo_log_path == str(log)
    assert (out / "UVU-KEO-101.pdf").is_file()
    assert (out / "UVU-KEO-102.pdf").is_file()
    assert content_hash(out / "UVU-KEO-101.pdf") == content_hash(source / "A-101.pdf")
    assert content_hash(out / "UVU-KEO-102.pdf") == content_hash(source / "A-102.pdf")

    undo_log = UndoLog.load(log)
    entries = undo_log.entries()
    assert len(entries) == 2
    assert [entry.success for entry in entries] == [True, True]
    assert [entry.operation for entry in entries] == ["copy", "copy"]
    assert entries[0].source_path == str(source / "A-101.pdf")
    assert entries[0].target_path == str(out / "UVU-KEO-101.pdf")
    assert entries[0].source_hash == content_hash(source / "A-101.pdf")


# ── Refusals ───────────────────────────────────────────────────────────


def test_executor_refuses_a_plan_that_cannot_apply(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    plan = build_plan(
        [sheet(_make(source, "drawing.pdf"))],
        "{number}",
        {"number": "CON"},
        tmp_path / "out",
    )
    assert not plan.can_apply

    with pytest.raises(ValueError, match="reserved_name"):
        execute_plan(plan, tmp_path / "rename_log.json")


def test_in_place_mode_refuses_without_i_understand(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    plan = build_plan(
        [sheet(_make(source, "A-101.pdf"), drawing_no="A-101")],
        TEMPLATE,
        CONTEXT,
        tmp_path / "out",
        mode=RenameMode.IN_PLACE,
    )
    assert plan.can_apply

    with pytest.raises(ValueError, match="i_understand"):
        execute_plan(plan, tmp_path / "rename_log.json", mode="in_place")


def test_copy_mode_refuses_a_target_inside_an_input_folder(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "A-101.pdf").write_bytes(b"drawing")
    plan = RenamePlan(
        actions=[
            RenameAction(
                source_path=str(source / "A-101.pdf"),
                target_path=str(source / "copy.pdf"),
            )
        ],
        mode=RenameMode.COPY,
        output_dir=str(source),
    )

    with pytest.raises(ValueError, match="inside an input folder"):
        execute_plan(plan, tmp_path / "rename_log.json", input_folders=[str(source)])


# ── In-place mode ──────────────────────────────────────────────────────


def test_in_place_moves_files_and_undo_restores_them(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    first = _make(source, "a1.pdf", content=b"first drawing")
    second = _make(source, "b2.pdf", content=b"second drawing")
    sheets = [
        sheet(first, drawing_no="A-101"),
        sheet(second, drawing_no="A-102"),
    ]
    plan = build_plan(sheets, TEMPLATE, CONTEXT, tmp_path / "out", mode=RenameMode.IN_PLACE)
    log = tmp_path / "rename_log.json"

    result = execute_plan(plan, log, mode="in_place", i_understand=True)

    assert result.completed == 2
    assert result.failed == []
    assert sorted(name.name for name in source.iterdir()) == [
        "UVU-KEO-101.pdf",
        "UVU-KEO-102.pdf",
    ]

    outcome = UndoLog(log).undo(verify=True)
    assert outcome.stopped is False
    assert outcome.reversed == 2
    assert outcome.failed == []
    assert sorted(name.name for name in source.iterdir()) == ["a1.pdf", "b2.pdf"]
    assert (source / "a1.pdf").read_bytes() == b"first drawing"
    assert (source / "b2.pdf").read_bytes() == b"second drawing"


def test_case_only_rename_goes_through_a_temp_name_and_undoes(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    original = _make(source, "abc.pdf", content=b"case content")
    plan = build_plan(
        [sheet(original)],
        "{number}",
        {"number": "ABC"},
        tmp_path / "out",
        mode=RenameMode.IN_PLACE,
    )
    assert plan.actions[0].status is ActionStatus.OK
    log = tmp_path / "rename_log.json"

    result = execute_plan(plan, log, mode="in_place", i_understand=True)

    assert result.completed == 1
    assert sorted(name.name for name in source.iterdir()) == ["ABC.pdf"]

    outcome = UndoLog(log).undo(verify=True)
    assert outcome.reversed == 1
    assert sorted(name.name for name in source.iterdir()) == ["abc.pdf"]
    assert (source / "abc.pdf").read_bytes() == b"case content"


# ── Failure isolation and cancel ───────────────────────────────────────


def test_individual_copy_failure_does_not_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "src"
    source.mkdir()
    plan = _copy_plan(source, tmp_path / "out", ["A-101.pdf", "A-102.pdf", "A-103.pdf"])

    original_copy2 = rename_executor.shutil.copy2
    calls = {"count": 0}

    def flaky_copy2(src: str, dst: str, *args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated disk failure")
        return original_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(rename_executor.shutil, "copy2", flaky_copy2)
    log = tmp_path / "rename_log.json"
    result = execute_plan(plan, log)

    assert result.completed == 2
    assert len(result.failed) == 1
    assert result.failed[0]["path"] == str(source / "A-102.pdf")
    assert "simulated disk failure" in str(result.failed[0]["error"])

    entries = UndoLog.load(log).entries()
    assert len(entries) == 3
    assert [entry.success for entry in entries] == [True, False, True]
    assert (tmp_path / "out" / "UVU-KEO-101.pdf").is_file()
    assert (tmp_path / "out" / "UVU-KEO-103.pdf").is_file()


def test_cancel_midway_leaves_a_valid_partial_log(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    plan = _copy_plan(source, tmp_path / "out", ["A-101.pdf", "A-102.pdf", "A-103.pdf"])
    token = CancelToken()

    def cancel_after_two(completed: int, total: int, name: str) -> None:
        if completed >= 2:
            token.cancel()

    log = tmp_path / "rename_log.json"
    result = execute_plan(plan, log, cancel=token, progress=cancel_after_two)

    assert result.cancelled is True
    assert result.completed == 2
    entries = UndoLog.load(log).entries()
    assert len(entries) == 2
    assert all(entry.success for entry in entries)
    assert (tmp_path / "out" / "UVU-KEO-101.pdf").is_file()
    assert (tmp_path / "out" / "UVU-KEO-102.pdf").is_file()
    assert not (tmp_path / "out" / "UVU-KEO-103.pdf").exists()


# ── Undo semantics ─────────────────────────────────────────────────────


def test_undo_removes_copies_and_never_touches_sources(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"
    plan = _copy_plan(source, out, ["A-101.pdf", "A-102.pdf", "A-103.pdf"])
    log = tmp_path / "rename_log.json"
    execute_plan(plan, log)

    outcome = UndoLog(log).undo(verify=True)

    assert outcome.stopped is False
    assert outcome.reversed == 3
    assert not any(out.iterdir())
    assert all((source / name).is_file() for name in ("A-101.pdf", "A-102.pdf", "A-103.pdf"))


def test_undo_refuses_when_a_copy_was_modified_externally(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    out = tmp_path / "out"
    plan = _copy_plan(source, out, ["A-101.pdf", "A-102.pdf"])
    log = tmp_path / "rename_log.json"
    execute_plan(plan, log)

    tampered = out / "UVU-KEO-102.pdf"  # the last entry reverses first
    with open(tampered, "ab") as handle:
        handle.write(b"tampered after the rename")

    outcome = UndoLog(log).undo(verify=True)

    assert outcome.stopped is True
    assert outcome.reversed == 0
    assert len(outcome.failed) == 1
    assert outcome.failed[0]["path"] == str(tampered)
    assert "changed since rename" in outcome.failed[0]["reason"]
    # The newer work was not overwritten and nothing else was deleted.
    assert tampered.is_file()
    assert (out / "UVU-KEO-101.pdf").is_file()
