"""API tests for the Phase 5 change list, and for the two tools being separate.

The point of these is not only that comparing works, but that it works
*without* the user walking a wizard: no output folder chosen by hand, no
matching review saved, no alignment screen visited.
"""

from __future__ import annotations

import time

from engine.core.models import SheetRecord
from engine.core.session import get_session, reset_session
from tests.fixture_builder import align_label_names, write_drawing_pdf


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


#: Text size for the room labels. Bigger than the fixture default so the
#: change is drawn at a size a real drawing would use — a 9 pt glyph edit
#: is mostly forgiven by the registration tolerance, and rightly so.
LABEL_SIZE = 14.0


def _labels(*, changed: bool) -> list[str]:
    """The room labels, with one renamed on the revised issue.

    A changed label rather than a changed `variant`: variants are entirely
    different drawings, which correctly refuse to align and so can never be
    compared. This is what a revision actually looks like — the same sheet,
    with one thing on it different.
    """
    labels = align_label_names()
    if changed:
        labels[5] = "ZZ-99"
    return labels


def _seed_folders(tmp_path, *, changed: bool = False) -> None:
    """Two issues of one drawing; `changed` renames a room on the new one."""
    reset_session()
    session = get_session()
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "new").mkdir(exist_ok=True)
    session.old.folder = str(tmp_path / "old")
    session.new.folder = str(tmp_path / "new")

    old_pdf = tmp_path / "old" / "A-101-RevC.pdf"
    new_pdf = tmp_path / "new" / "A-101-RevD.pdf"
    write_drawing_pdf(
        old_pdf,
        drawing_no="A-101",
        revision="C",
        labels=_labels(changed=False),
        label_size=LABEL_SIZE,
    )
    write_drawing_pdf(
        new_pdf,
        drawing_no="A-101",
        revision="D",
        labels=_labels(changed=changed),
        label_size=LABEL_SIZE,
    )
    session.old.sheets.append(_sheet(old_pdf, "A-101", "C", "old"))
    session.new.sheets.append(_sheet(new_pdf, "A-101", "D", "new"))


def _poll(client, path: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(path).json()
        if status["state"] in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.3)
    raise AssertionError(f"{path} never settled")


def _compare_to_completion(client, tmp_path) -> dict:
    """Run the standalone tool right through, however many stages it needs."""
    first = client.post("/api/compare/start")
    assert first.status_code == 200, first.json()
    if first.json()["stage"] == "aligning":
        assert _poll(client, "/api/align/status")["state"] == "done"
        second = client.post("/api/compare/start")
        assert second.status_code == 200, second.json()
        assert second.json()["stage"] == "comparing"
    return _poll(client, "/api/compare/status")


# ── The tools are separate ─────────────────────────────────────────────


def test_compare_needs_no_output_folder_chosen_by_hand(client, tmp_path, monkeypatch):
    """The whole reason the viewer used to come up blank."""
    _seed_folders(tmp_path)
    session = get_session()
    assert session.workspace is None

    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    status = _compare_to_completion(client, tmp_path)

    assert status["state"] == "done", status
    assert get_session().workspace is not None


def test_compare_needs_no_matching_review(client, tmp_path, monkeypatch):
    """Matching runs as pairing, not as a gate the user has to pass."""
    _seed_folders(tmp_path)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    session = get_session()
    assert session.match_result is None
    assert session.match_decisions == {}

    status = _compare_to_completion(client, tmp_path)

    assert status["state"] == "done", status
    # Matching happened on the way, without the user ever saving a review.
    assert get_session().match_result is not None
    assert get_session().match_decisions == {}


def test_tiles_create_the_workspace_rather_than_refusing(client, tmp_path, monkeypatch):
    """A blank viewer with a silent 422 was the old behaviour."""
    _seed_folders(tmp_path)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    session = get_session()
    from engine.api.routes_tiles import sheet_id

    target = sheet_id(session.new.sheets[0].abs_path, 0)
    response = client.get(f"/api/tiles/{target}/manifest")

    assert response.status_code == 200, response.json()
    assert response.json()["width_px"] > 0


# ── The change list ────────────────────────────────────────────────────


def test_an_unchanged_sheet_reports_no_changes(client, tmp_path, monkeypatch):
    """The plan's acceptance test: only the revision letter differs."""
    _seed_folders(tmp_path)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    assert _compare_to_completion(client, tmp_path)["state"] == "done"

    results = client.get("/api/compare/results").json()["results"]
    assert len(results) == 1
    assert results[0]["failure"] is None
    assert results[0]["substantive_count"] == 0


def test_a_changed_sheet_reports_its_changes(client, tmp_path, monkeypatch):
    _seed_folders(tmp_path, changed=True)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    assert _compare_to_completion(client, tmp_path)["state"] == "done"

    results = client.get("/api/compare/results").json()["results"]
    assert results[0]["region_count"] > 0
    for region in results[0]["regions"]:
        assert region["explanation"]
        assert region["severity"] in {"critical", "major", "minor", "trivial"}
        assert region["type"] in {"added", "removed", "moved", "modified", "cosmetic"}
        # Measurements reach the user in millimetres, never as pixels.
        assert region["area_mm2"] >= 0
        assert len(region["bbox_mm"]) == 4


def test_the_change_list_carries_the_text_that_changed(client, tmp_path, monkeypatch):
    """The serialiser must not drop the fields severity is decided by."""
    _seed_folders(tmp_path, changed=True)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    assert _compare_to_completion(client, tmp_path)["state"] == "done"

    regions = client.get("/api/compare/results").json()["results"][0]["regions"]
    for region in regions:
        assert "text_kind" in region
        assert "old_text" in region
        assert "new_text" in region

    # The renamed room is reported in words, not as an area, and the words
    # are what the severity was decided from.
    renamed = [region for region in regions if region["new_text"] == "ZZ-99"]
    assert renamed, regions
    assert renamed[0]["old_text"] == "RM-06"
    assert renamed[0]["text_kind"] == "tag"
    assert renamed[0]["severity"] == "major"
    assert not renamed[0]["is_cosmetic"]
    assert "RM-06" in renamed[0]["explanation"]


def test_the_run_is_snapshotted_into_the_audit_folder(client, tmp_path, monkeypatch):
    _seed_folders(tmp_path, changed=True)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    assert _compare_to_completion(client, tmp_path)["state"] == "done"
    assert (tmp_path / "workspace" / "_audit" / "changes.json").is_file()


def test_results_require_a_finished_run(client):
    reset_session()
    response = client.get("/api/compare/results")
    assert response.status_code == 422
    assert "error" in response.json()


def test_comparing_with_nothing_aligned_is_refused(client, tmp_path):
    """Never diff two unaligned sheets: it would flag every line."""
    _seed_folders(tmp_path)
    response = client.post("/api/compare/run")
    assert response.status_code == 422
    assert "error" in response.json()


# ── One pair on its own ────────────────────────────────────────────────


def test_a_single_pair_can_be_compared_directly(client, tmp_path, monkeypatch):
    _seed_folders(tmp_path, changed=True)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    session = get_session()
    from engine.api.routes_tiles import sheet_id

    response = client.post(
        "/api/compare/pair",
        json={
            "old_sheet_id": sheet_id(session.old.sheets[0].abs_path, 0),
            "new_sheet_id": sheet_id(session.new.sheets[0].abs_path, 0),
        },
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["failure"] is None
    assert body["region_count"] >= 0


def test_an_unknown_sheet_is_refused(client, tmp_path, monkeypatch):
    _seed_folders(tmp_path)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    response = client.post(
        "/api/compare/pair",
        json={"old_sheet_id": "0" * 16, "new_sheet_id": "1" * 16},
    )
    assert response.status_code == 422


# ── The output folder round-trips ──────────────────────────────────────


def test_the_chosen_output_folder_can_be_read_back(client, tmp_path):
    """Without this the UI could never restore it after a reload."""
    reset_session()
    assert client.get("/api/output").json()["folder"] is None

    get_session().set_output_folder(str(tmp_path / "workspace"))
    body = client.get("/api/output").json()

    assert body["folder"] == str(tmp_path / "workspace")
    assert body["workspace"] is not None


# ── Starting over really starts over ───────────────────────────────────


def test_reset_clears_every_stage(client, tmp_path, monkeypatch):
    """A stale match or alignment surviving a reset is quiet wrongness."""
    _seed_folders(tmp_path, changed=True)
    monkeypatch.setattr("engine.core.session.suggest_output", lambda _s: tmp_path / "workspace")
    assert _compare_to_completion(client, tmp_path)["state"] == "done"

    client.post("/api/session/reset")

    session = get_session()
    assert session.match_result is None
    assert session.align_results == []
    assert session.compare_results == []
    assert session.workspace is None
    assert session.compare_run["state"] == "idle"
    assert session.align_run["state"] == "idle"
