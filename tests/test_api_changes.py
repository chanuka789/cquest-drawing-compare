"""API tests for the Phase 5 comparison run (align -> compare -> report).

Includes the route-ordering trap the project has been bitten by before:
a literal path declared after a parameterised one is swallowed by it, so
`/api/compare/status` arrives as a pair id called "status".
"""

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


def _seed(tmp_path) -> None:
    """One pair whose new sheet differs by its revision letter alone."""
    reset_session()
    session = get_session()
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "new").mkdir(exist_ok=True)
    session.old.folder = str(tmp_path / "old")
    session.new.folder = str(tmp_path / "new")
    session.set_output_folder(str(tmp_path / "workspace"))

    labels = ["RM-01", "RM-02", "3000", "2400", "D-12", "W3"]
    old_pdf = tmp_path / "old" / "A-101-RevC.pdf"
    new_pdf = tmp_path / "new" / "A-101-RevD.pdf"
    write_drawing_pdf(old_pdf, drawing_no="A-101", revision="C", labels=labels)
    write_drawing_pdf(
        new_pdf, drawing_no="A-101", revision="D", labels=[*labels[:2], "3200", *labels[3:]]
    )

    old = _sheet(old_pdf, "A-101", "C", "old")
    new = _sheet(new_pdf, "A-101", "D", "new")
    session.old.sheets.append(old)
    session.new.sheets.append(new)
    session.match_result = MatchResult(
        pairs=[MatchPair(old=old, new=new, confidence=1.0, tier="exact_number", reason="test")]
    )
    session.apply_match_decisions(accepted=[f"{old.abs_path}#0"])


def _poll(client, path: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(path).json()
        if status["state"] in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.3)
    raise AssertionError(f"{path} never finished")


def _run(client, tmp_path) -> dict:
    _seed(tmp_path)
    assert client.post("/api/align/run").status_code == 200
    assert _poll(client, "/api/align/status")["state"] == "done"

    response = client.post("/api/compare/queue", json={"pair_ids": []})
    assert response.status_code == 200, response.json()
    return _poll(client, "/api/compare/status")


def test_comparison_round_trip(client, tmp_path):
    status = _run(client, tmp_path)

    assert status["state"] == "done", status
    assert status["summary"]["pairs"] == 1

    results = client.get("/api/compare/results").json()
    assert len(results["results"]) == 1
    pair_id = results["results"][0]["pair_id"]

    changes = client.get(f"/api/changes/{pair_id}").json()
    assert changes["pair_id"] == pair_id
    descriptions = [change["description"] for change in changes["changes"]]
    assert any("3000" in text and "3200" in text for text in descriptions), descriptions


def test_a_change_is_reported_in_millimetres_never_pixels(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]
    payload = client.get(f"/api/changes/{pair_id}").json()

    assert payload["tolerance"]["position"]["paper_mm"] > 0
    assert payload["tolerance"]["position"]["site_mm"] == 25.0
    for change in payload["changes"]:
        assert set(change["bbox_mm"]) == {"x", "y", "w", "h"}


def test_the_revision_letter_never_reaches_the_report(client, tmp_path):
    """The gate, through the API this time."""
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]
    payload = client.get(f"/api/changes/{pair_id}").json()

    for change in payload["changes"]:
        text = change.get("text") or {}
        assert text.get("old_text") != "C", change
        assert change["category"] != "titleblock"


def test_cosmetic_changes_are_hidden_unless_asked_for(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    default = client.get(f"/api/changes/{pair_id}").json()["changes"]
    everything = client.get(f"/api/changes/{pair_id}", params={"include_cosmetic": True}).json()[
        "changes"
    ]

    assert all(not change["is_cosmetic"] for change in default)
    assert len(everything) >= len(default)


def test_changes_can_be_filtered_by_stream_and_kind(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    text_only = client.get(f"/api/changes/{pair_id}", params={"stream": "text"}).json()
    assert all("text" in change["streams"] for change in text_only["changes"])

    modified = client.get(f"/api/changes/{pair_id}", params={"kind": "modified"}).json()
    assert all(change["kind"] == "modified" for change in modified["changes"])


def test_what_was_suppressed_is_retrievable_with_its_reason(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    payload = client.get(f"/api/changes/{pair_id}/filtered").json()
    assert payload["pair_id"] == pair_id
    for entry in payload["filtered"]:
        assert entry["filter"]
        assert entry["reason"]


def test_stats_carry_the_streams_timings_and_config(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    stats = client.get(f"/api/compare/{pair_id}/stats").json()
    assert stats["pair_id"] == pair_id
    assert {entry["stream"] for entry in stats["streams"]} == {
        "text",
        "vector",
        "hatch",
        "raster",
    }
    assert stats["timings"]
    assert stats["engine_version"]
    assert stats["config"]["dpi"] > 0


def test_a_user_can_triage_a_change(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    response = client.patch(f"/api/changes/{pair_id}/0", json={"user_status": "confirmed"})
    assert response.status_code == 200
    assert response.json()["user_status"] == "confirmed"


def test_an_invalid_status_is_refused_with_the_allowed_values(client, tmp_path):
    _run(client, tmp_path)
    pair_id = client.get("/api/compare/results").json()["results"][0]["pair_id"]

    response = client.patch(f"/api/changes/{pair_id}/0", json={"user_status": "maybe"})
    assert response.status_code == 422
    assert "confirmed" in response.json()["error"]["message"]


def test_an_unknown_pair_is_a_clean_404(client, tmp_path):
    _run(client, tmp_path)
    response = client.get("/api/changes/pair-does-not-exist")

    assert response.status_code == 404
    assert "not been compared" in response.json()["error"]["message"]


def test_the_literal_status_route_is_not_swallowed_by_a_parameter(client):
    """`/api/compare/status` must not arrive as a pair id called "status"."""
    reset_session()
    response = client.get("/api/compare/status")

    assert response.status_code == 200
    assert response.json()["state"] == "idle"


def test_comparing_before_aligning_is_refused_with_advice(client):
    reset_session()
    response = client.post("/api/compare/queue", json={"pair_ids": []})

    assert response.status_code == 422
    assert "Align the drawings" in response.json()["error"]["message"]


def test_cancelling_an_idle_run_is_harmless(client):
    reset_session()
    assert client.post("/api/compare/cancel").json() == {"cancelled": False}
