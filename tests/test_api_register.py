"""The Phase 2 API, end to end: folders in, register and export out."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.core.session import get_session, reset_session
from tests.fixture_builder import (
    build_messy_drawing_list,
    build_normal_pair,
    build_partial_issue_pair,
)


@pytest.fixture(autouse=True)
def fresh_session():
    reset_session()
    yield
    reset_session()


@pytest.fixture(scope="module")
def normal_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return build_normal_pair(tmp_path_factory.mktemp("api-normal"))


def scan_both(client: TestClient, old_dir: Path, new_dir: Path) -> None:
    """Choose both folders and let the deep pass finish."""
    for side, folder in (("old", old_dir), ("new", new_dir)):
        response = client.post("/api/folder", json={"side": side, "folder": str(folder)})
        assert response.status_code == 200, response.text
        assert client.post(f"/api/scan/{side}").status_code == 200

    get_session().wait_for_scans()


# ── Choosing folders ───────────────────────────────────────────────────


def test_choosing_a_folder_returns_the_fast_pass_immediately(client, normal_pair):
    old_dir, _ = normal_pair

    response = client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})

    assert response.status_code == 200
    body = response.json()
    assert body["side"] == "old"
    assert body["file_count"] == 10
    assert body["sheet_count"] == 0  # nothing opened yet
    assert "10 files" in body["headline"]


def test_the_same_folder_on_both_sides_is_refused(client, normal_pair):
    old_dir, _ = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})

    response = client.post("/api/folder", json={"side": "new", "folder": str(old_dir)})

    assert response.status_code == 422
    assert "same folder" in response.json()["error"]["message"]


def test_a_missing_folder_is_reported_as_structured_json(client, tmp_path: Path):
    response = client.post("/api/folder", json={"side": "old", "folder": str(tmp_path / "nope")})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_scanning_before_choosing_a_folder_says_what_to_do(client):
    response = client.post("/api/scan/old")

    assert response.status_code == 422
    assert "Choose a folder" in response.json()["error"]["message"]


def test_the_deep_pass_fills_in_the_sheets(client, normal_pair):
    old_dir, new_dir = normal_pair
    scan_both(client, old_dir, new_dir)

    sides = {item["side"]: item for item in client.get("/api/sides").json()}
    assert sides["old"]["sheet_count"] == 10
    assert sides["old"]["identified_count"] == 10
    assert not sides["old"]["is_scanning"]

    sheets = client.get("/api/sheets/old").json()
    numbers = {sheet["drawing_no"] for sheet in sheets}
    assert "A-101" in numbers
    assert all(sheet["source_of_number"] == "titleblock" for sheet in sheets)
    assert sheets[0]["source_explanation"]


def test_sheets_are_listed_from_the_fast_pass_before_the_deep_one(client, normal_pair):
    """Rows appear at once and fill in with detail as results stream."""
    old_dir, _ = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})

    sheets = client.get("/api/sheets/old").json()

    assert len(sheets) == 10
    assert all(sheet["drawing_no"] is None for sheet in sheets)  # not read yet
    assert all(sheet["filename"] for sheet in sheets)


def test_cancelling_a_scan_is_accepted(client, normal_pair):
    old_dir, _ = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})
    client.post("/api/scan/old")

    assert client.post("/api/scan/cancel").json() == {"cancelled": True}
    get_session().wait_for_scans()


# ── The output folder ──────────────────────────────────────────────────


def test_an_output_folder_inside_an_input_folder_is_refused(client, normal_pair):
    old_dir, new_dir = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})
    client.post("/api/folder", json={"side": "new", "folder": str(new_dir)})

    response = client.post("/api/output", json={"folder": str(new_dir / "results")})

    assert response.status_code == 422
    assert "inside the current issue folder" in response.json()["error"]["message"]


def test_a_valid_output_folder_creates_the_workspace(client, normal_pair, tmp_path: Path):
    old_dir, new_dir = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})
    client.post("/api/folder", json={"side": "new", "folder": str(new_dir)})

    output = tmp_path / "Compare_RevC_to_RevD"
    response = client.post("/api/output", json={"folder": str(output)})

    assert response.status_code == 200
    assert (output / "01_Register").is_dir()
    assert (output / "_audit").is_dir()


def test_an_output_folder_is_suggested_once_both_are_chosen(client, normal_pair):
    old_dir, new_dir = normal_pair
    assert client.get("/api/output/suggestion").json()["folder"] is None

    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})
    client.post("/api/folder", json={"side": "new", "folder": str(new_dir)})

    suggestion = client.get("/api/output/suggestion").json()["folder"]
    assert suggestion is not None
    assert "Compare_" in suggestion


# ── Options ────────────────────────────────────────────────────────────


def test_profiles_are_listed(client):
    ids = {entry["id"] for entry in client.get("/api/profiles").json()}
    assert {"default", "keo"} <= ids


def test_options_are_stored(client):
    response = client.post("/api/options", json={"profile_id": "keo", "tolerance_mm": 50})

    assert response.json() == {"profile_id": "keo", "tolerance_mm": 50.0}
    assert get_session().profile_id == "keo"


def test_a_tolerance_of_zero_is_refused(client):
    response = client.post("/api/options", json={"tolerance_mm": 0})

    assert response.status_code == 422
    assert "greater than zero" in response.json()["error"]["message"]


# ── The register ───────────────────────────────────────────────────────


def test_building_the_register_before_scanning_says_what_to_do(client):
    response = client.post("/api/register", json={"issue_type": "full"})

    assert response.status_code == 422
    assert "Choose both folders" in response.json()["error"]["message"]


def test_the_register_is_built(client, normal_pair):
    old_dir, new_dir = normal_pair
    scan_both(client, old_dir, new_dir)

    response = client.post("/api/register", json={"issue_type": "full"})

    assert response.status_code == 200
    body = response.json()
    assert not body["needs_issue_type_confirmation"]

    rows = {row["drawing_no"]: row for row in body["rows"]}
    assert rows["A-101"]["status"] == "revised"
    assert rows["A-303"]["status"] == "new"
    assert body["summary"]["counts"]["revised"] == 3
    assert "revised" in body["summary"]["sentence"]


def test_a_partial_issue_asks_before_producing_a_register(client, tmp_path: Path):
    """The question comes back instead of a register full of false removals."""
    old_dir, new_dir = build_partial_issue_pair(tmp_path / "api-partial", old_count=20)
    scan_both(client, old_dir, new_dir)

    response = client.post("/api/register", json={"issue_type": "unknown"})
    body = response.json()

    assert body["needs_issue_type_confirmation"]
    assert body["rows"] == []

    question = body["issue_type_question"]
    assert question["old_count"] == 20
    assert question["new_count"] == 3
    assert {option["id"] for option in question["options"]} == {
        "partial",
        "full",
        "compare_reissued",
    }

    answered = client.post("/api/register", json={"issue_type": "partial"}).json()
    statuses = {row["status"] for row in answered["rows"]}
    assert "removed" not in statuses


# ── Correcting a drawing number ────────────────────────────────────────


def test_a_drawing_number_can_be_corrected_by_hand(client, tmp_path: Path):
    from tests.fixture_builder import SheetSpec, build_pdf

    folder = tmp_path / "unidentified"
    build_pdf(
        folder / "scan001.pdf",
        [SheetSpec(include_title_block=False, body=["NOTHING USEFUL"])],
    )
    client.post("/api/folder", json={"side": "new", "folder": str(folder)})
    client.post("/api/scan/new")
    get_session().wait_for_scans()

    sheets = client.get("/api/sheets/new").json()
    assert sheets[0]["drawing_no"] is None

    response = client.post(
        "/api/register/correct",
        json={"abs_path": sheets[0]["abs_path"], "page_index": 0, "drawing_no": "A-101"},
    )

    assert response.json()["corrected"] == 1
    assert client.get("/api/sheets/new").json()[0]["drawing_no"] == "A-101"
    assert client.get("/api/sheets/new").json()[0]["source_of_number"] == "user"


def test_correcting_a_sheet_that_is_gone_says_so(client):
    response = client.post(
        "/api/register/correct",
        json={"abs_path": "D:\\nowhere.pdf", "page_index": 0, "drawing_no": "A-101"},
    )

    assert response.status_code == 422
    assert "no longer in the scan" in response.json()["error"]["message"]


# ── The drawing list ───────────────────────────────────────────────────


def test_a_drawing_list_is_previewed_before_it_is_used(client, tmp_path: Path):
    path = build_messy_drawing_list(tmp_path / "register_messy.xlsx")

    body = client.post("/api/drawing-list/preview", json={"path": str(path)}).json()

    assert body["ok"]
    assert body["header_row"] == 7
    assert body["mapping"]["drawing_no"] == "Dwg No."
    assert len(body["preview"]) == 10
    assert get_session().drawing_list == []  # nothing used until confirmed


def test_confirming_the_mapping_loads_the_list(client, tmp_path: Path):
    path = build_messy_drawing_list(tmp_path / "register_messy.xlsx")
    client.post("/api/drawing-list/preview", json={"path": str(path)})

    response = client.post(
        "/api/drawing-list/mapping",
        json={"mapping": {"drawing_no": "Dwg No.", "title": "Sheet Name", "revision": "Rev."}},
    )

    assert response.status_code == 200
    assert len(get_session().drawing_list) == 10


def test_setting_a_mapping_without_a_list_says_what_to_do(client):
    response = client.post("/api/drawing-list/mapping", json={"mapping": {}})

    assert response.status_code == 422
    assert "Import a drawing list" in response.json()["error"]["message"]


def test_the_drawing_list_can_be_removed(client, tmp_path: Path):
    path = build_messy_drawing_list(tmp_path / "list.xlsx")
    client.post("/api/drawing-list/preview", json={"path": str(path)})
    client.post("/api/drawing-list/mapping", json={"mapping": {"drawing_no": "Dwg No."}})

    assert client.delete("/api/drawing-list").json() == {"cleared": True}
    assert get_session().drawing_list == []


# ── Export ─────────────────────────────────────────────────────────────


def test_exporting_without_an_output_folder_says_what_to_do(client, normal_pair):
    old_dir, new_dir = normal_pair
    scan_both(client, old_dir, new_dir)
    client.post("/api/register", json={"issue_type": "full"})

    response = client.post("/api/register/export")

    assert response.status_code == 422
    assert "output folder" in response.json()["error"]["message"]


def test_the_register_exports_into_the_workspace(client, normal_pair, tmp_path: Path):
    old_dir, new_dir = normal_pair
    scan_both(client, old_dir, new_dir)

    output = tmp_path / "Compare"
    client.post("/api/output", json={"folder": str(output)})
    client.post("/api/register", json={"issue_type": "full"})

    response = client.post("/api/register/export")

    assert response.status_code == 200
    written = Path(response.json()["path"])
    assert written.is_file()
    assert written.parent == output / "01_Register"

    # The audit log is what makes the output defensible.
    audit = output / "_audit" / "run_log.json"
    assert audit.is_file()

    import json

    payload = json.loads(audit.read_text(encoding="utf-8"))
    assert payload["issue_type"] == "full"
    assert payload["new_sheet_count"] == 11


def test_starting_a_new_comparison_clears_the_session(client, normal_pair):
    old_dir, _ = normal_pair
    client.post("/api/folder", json={"side": "old", "folder": str(old_dir)})

    assert client.post("/api/session/reset").json() == {"reset": True}
    assert client.get("/api/sides").json()[0]["folder"] is None
