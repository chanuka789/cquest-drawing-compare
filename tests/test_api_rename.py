"""API tests for the rename flow: plan -> apply (copy) -> status -> undo,
with the SQLite mirror verified alongside the JSON log."""

from __future__ import annotations

import time

from engine.core.models import SheetRecord
from engine.core.session import get_session, reset_session
from engine.storage.db import open_project_db, session_scope
from engine.storage.schema import RenameLog
from tests.fixture_builder import SheetSpec, build_pdf

TEMPLATE = "{original}_ISSUED"


def _sheets_from(folder) -> list[SheetRecord]:
    sheets: list[SheetRecord] = []
    for pdf in sorted(folder.glob("*.pdf")):
        sheets.append(
            SheetRecord(
                side="new",
                abs_path=str(pdf),
                filename=pdf.name,
                page_count=1,
                drawing_no=None,
                is_readable=True,
            )
        )
    return sheets


def _seed(tmp_path) -> None:
    reset_session()
    session = get_session()
    for index in range(3):
        build_pdf(
            tmp_path / "new" / f"UVU-ARC-00{index + 1}.pdf",
            [SheetSpec(drawing_no=f"UVU-ARC-00{index + 1}", body=["SHEET CONTENT"])],
        )
    session.new.folder = str(tmp_path / "new")
    session.new.sheets = _sheets_from(tmp_path / "new")
    session.set_output_folder(str(tmp_path / "workspace"))


def _poll_until_done(client) -> dict:
    for _ in range(120):
        status = client.get("/api/rename/status").json()
        if status["state"] in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.1)
    raise AssertionError("rename/undo never finished")


def test_rename_plan_apply_undo_round_trip(client, tmp_path):
    _seed(tmp_path)
    session = get_session()
    workspace = session.workspace
    assert workspace is not None

    plan = client.post(
        "/api/rename/plan",
        json={"template": TEMPLATE, "mode": "copy", "use_discipline_folders": False},
    )
    assert plan.status_code == 200
    body = plan.json()
    assert body["can_apply"] is True
    assert len(body["actions"]) == 3
    assert body["summary"].get("to_change") == 3
    assert body["preview"]  # live preview of real names

    started = client.post("/api/rename/apply", json={"i_understand": False})
    assert started.status_code == 200
    status = _poll_until_done(client)
    assert status["state"] == "done", status
    assert status["completed"] == 3

    copied = sorted(workspace.renamed_dir.glob("*_ISSUED.pdf"))
    assert len(copied) == 3
    inputs_untouched = sorted((tmp_path / "new").glob("UVU-ARC-00*.pdf"))
    assert len(inputs_untouched) == 3

    # JSON audit log exists…
    log_path = workspace.audit_dir / "rename_log.json"
    assert log_path.is_file()
    # …and the SQLite mirror holds one row per settled entry.
    import json

    entries = json.loads(log_path.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 3
    assert all(entry["success"] for entry in entries)

    db_path = workspace.audit_dir / "project.cqdc"
    assert db_path.is_file()
    engine = open_project_db(db_path)
    with session_scope(engine) as db_session:
        assert db_session.query(RenameLog).count() == 3
    engine.dispose()

    # Undo removes only the copies; the originals never move.
    undone = client.post("/api/rename/undo")
    assert undone.status_code == 200
    status = _poll_until_done(client)
    assert status["state"] == "done", status
    assert not list(workspace.renamed_dir.glob("*.pdf"))
    assert len(list((tmp_path / "new").glob("UVU-ARC-00*.pdf"))) == 3


def test_apply_without_a_plan_is_rejected(client):
    reset_session()
    response = client.post("/api/rename/apply", json={"i_understand": False})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_plan_requires_drawings_on_the_current_issue(client):
    reset_session()
    response = client.post(
        "/api/rename/plan",
        json={"template": TEMPLATE, "mode": "copy", "use_discipline_folders": False},
    )
    assert response.status_code == 422
