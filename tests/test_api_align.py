"""API tests for the Phase 4 alignment run (match review -> align -> results)."""

from __future__ import annotations

import time

from engine.core.models import SheetRecord
from engine.core.session import get_session, reset_session
from engine.naming.matcher import MatchPair, MatchResult
from tests.fixture_builder import write_drawing_pdf


def _sheet(path, drawing_no: str, revision: str, side: str) -> SheetRecord:
    return SheetRecord(
        side=side,
        abs_path=str(path),
        filename=path.name,
        page_index=0,
        page_count=1,
        drawing_no=drawing_no,
        revision=revision,
        scale="1 : 100",
        is_readable=True,
    )


def _seed_one_identity_pair(tmp_path, number: str, session) -> tuple[SheetRecord, SheetRecord]:
    old_pdf = tmp_path / "old" / f"{number}-RevC.pdf"
    new_pdf = tmp_path / "new" / f"{number}-RevD.pdf"
    write_drawing_pdf(old_pdf, drawing_no=number, revision="C", variant=0)
    write_drawing_pdf(new_pdf, drawing_no=number, revision="D", variant=0)
    old = _sheet(old_pdf, number, "C", "old")
    new = _sheet(new_pdf, number, "D", "new")
    session.old.sheets.append(old)
    session.new.sheets.append(new)
    return old, new


def _seed(client, tmp_path) -> None:
    reset_session()
    session = get_session()
    session.old.folder = str(tmp_path / "old")
    session.new.folder = str(tmp_path / "new")
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "new").mkdir(exist_ok=True)
    session.set_output_folder(str(tmp_path / "workspace"))

    pairs = []
    for number in ("A-101", "A-102"):
        old, new = _seed_one_identity_pair(tmp_path, number, session)
        pairs.append(
            MatchPair(old=old, new=new, confidence=1.0, tier="exact_number", reason="test")
        )
    session.match_result = MatchResult(pairs=pairs)
    old_key = f"{pairs[0].old.abs_path}#0"
    session.apply_match_decisions(accepted=[old_key, f"{pairs[1].old.abs_path}#0"])


def _poll_align(client, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/align/status").json()
        if status["state"] in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.3)
    raise AssertionError("alignment never finished")


def test_alignment_round_trip(client, tmp_path):
    _seed(client, tmp_path)

    response = client.post("/api/align/run")
    assert response.status_code == 200
    assert response.json()["run_id"]

    status = _poll_align(client)
    assert status["state"] == "done", status
    assert status["total"] == 2
    assert status["current"] == 2
    assert status["summary"].get("excellent") == 2 or status["summary"].get("good") == 2

    results = client.get("/api/align/results").json()
    assert len(results["results"]) == 2
    for row in results["results"]:
        assert row["verdict"] in {"good", "excellent"}
        assert row["matrix"] is not None
        assert row["explanation"]

    # The audit snapshot exists in the workspace.
    audit = tmp_path / "workspace" / "_audit" / "alignment.json"
    assert audit.is_file()


def test_align_results_require_a_finished_run(client):
    reset_session()
    response = client.get("/api/align/results")
    assert response.status_code == 422
    assert "error" in response.json()
