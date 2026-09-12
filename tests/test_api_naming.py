"""API tests for the Phase 3 matching review flow.

The rename endpoints are covered in test_api_rename.py; this file pins the
match run -> status -> result -> decisions -> finalize round trip.
"""

from __future__ import annotations

import json

from engine.core.models import SheetRecord
from engine.core.session import get_session, reset_session
from tests.fixture_builder import SheetSpec, build_pdf


def _sheet(path, index: int = 0) -> SheetRecord:
    return SheetRecord(
        side="old",
        abs_path=str(path),
        filename=path.name,
        page_index=index,
        page_count=1,
        drawing_no=None,
        is_readable=True,
    )


def _seed_session(tmp_path) -> None:
    reset_session()
    session = get_session()

    drawings = [
        ("UVU-ARC-001", ["ROOM 101 LAYOUT", "WALL W1 200MM", "CEILING 3000"]),
        ("UVU-ARC-002", ["ROOF DRAINAGE", "FALL 1 IN 60", "OUTLET 2000"]),
    ]
    mapping = {
        "UVU-ARC-001": "UVU-KEO-XX-03-DR-A-0001",
        "UVU-ARC-002": "UVU-KEO-XX-03-DR-A-0002",
    }
    for number, body in drawings:
        build_pdf(
            tmp_path / "old" / f"{number}.pdf",
            [SheetSpec(drawing_no=number, title=number, body=body)],
        )
        build_pdf(
            tmp_path / "new" / f"{mapping[number]}.pdf",
            [SheetSpec(drawing_no=mapping[number], title=number, body=body)],
        )

    session.old.sheets = [_sheet(path) for path in sorted((tmp_path / "old").glob("*.pdf"))]
    session.new.sheets = [_sheet(path) for path in sorted((tmp_path / "new").glob("*.pdf"))]


def test_matching_round_trip(client, tmp_path):
    _seed_session(tmp_path)
    session = get_session()

    response = client.post("/api/match/run")
    assert response.status_code == 200
    assert response.json()["run_id"]

    session.wait_for_matching(timeout=120)

    status = client.get("/api/match/status").json()
    assert status["state"] == "ready"

    result = client.get("/api/match/result").json()
    assert len(result["pairs"]) == 2
    assert result["summary"]["auto"] == 2
    assert result["summary"]["old_unmatched"] == 0
    assert not result["old_unmatched"]
    assert not result["new_unmatched"]

    # Accept every pair by old key, then finalize into the workspace audit.
    keys = [pair["old"]["key"] for pair in result["pairs"]]
    decisions = client.post(
        "/api/match/decisions", json={"accepted": keys, "rejected": [], "manual": []}
    )
    assert decisions.status_code == 200
    assert decisions.json()["unresolved_review"] == 0

    session.set_output_folder(str(tmp_path / "workspace"))
    finalized = client.post("/api/match/finalize")
    assert finalized.status_code == 200
    path = finalized.json()["path"]
    assert path and json.loads(__import__("pathlib").Path(path).read_text(encoding="utf-8"))


def test_match_result_requires_a_finished_run(client):
    reset_session()
    response = client.get("/api/match/result")
    assert response.status_code == 422
    assert "error" in response.json()
