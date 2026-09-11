"""API tests for sheet templates and the mask editor.

The flow under test is the one the whole masking design exists for: detect
once per template, confirm once, apply to every sheet in the cluster.
"""

from __future__ import annotations

from engine.core.models import SheetRecord
from engine.core.session import get_session, reset_session
from tests.fixture_builder import write_drawing_pdf


def _seed(tmp_path, count: int = 4) -> None:
    reset_session()
    session = get_session()
    folder = tmp_path / "new"
    folder.mkdir(exist_ok=True)
    session.new.folder = str(folder)

    for index in range(count):
        path = folder / f"A-10{index}.pdf"
        write_drawing_pdf(
            path,
            drawing_no=f"A-10{index}",
            revision="D",
            labels=["RM-01", "3000", "D-12"],
        )
        session.new.sheets.append(
            SheetRecord(
                side="new",
                abs_path=str(path),
                filename=path.name,
                page_index=0,
                page_count=1,
                drawing_no=f"A-10{index}",
                revision="D",
                scale="1 : 100",
                is_readable=True,
            )
        )


def test_sheets_cluster_into_templates_with_zones(client, tmp_path):
    _seed(tmp_path)
    payload = client.get("/api/mask/templates").json()

    assert payload["sheet_count"] == 4
    assert len(payload["templates"]) == 1

    template = payload["templates"][0]
    assert template["sheet_count"] == 4
    assert template["representative_sheet_id"] in template["member_sheet_ids"]
    assert not template["confirmed"]
    assert any(zone["type"] == "titleblock" for zone in template["zones"])


def test_every_zone_explains_itself(client, tmp_path):
    _seed(tmp_path)
    template = client.get("/api/mask/templates").json()["templates"][0]

    for zone in template["zones"]:
        assert zone["label"], zone
        assert zone["evidence"], zone
        assert 0.0 <= zone["confidence"] <= 1.0


def test_protected_regions_are_listed_separately(client, tmp_path):
    _seed(tmp_path)
    template = client.get("/api/mask/templates").json()["templates"][0]

    # The fixture has no notes or north arrow, but the key is that the list
    # exists and is distinct from the exclusions.
    assert "protected" in template
    assert isinstance(template["protected"], list)


def test_confirming_a_mask_applies_it_to_the_whole_cluster(client, tmp_path):
    _seed(tmp_path)
    template_id = client.get("/api/mask/templates").json()["templates"][0]["template_id"]

    response = client.put(
        f"/api/mask/{template_id}",
        json={
            "zones": [
                {
                    "type": "titleblock",
                    "rect": {"x0": 0.7, "y0": 0.85, "x1": 1.0, "y1": 1.0},
                    "label": "Title block",
                }
            ],
            "protected": [
                {
                    "type": "general_notes",
                    "rect": {"x0": 0.05, "y0": 0.2, "x1": 0.25, "y1": 0.6},
                    "label": "General notes",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied_to"] == 4
    assert body["confirmed"]

    stored = client.get(f"/api/mask/{template_id}").json()
    assert stored["confirmed"]
    assert len(stored["zones"]) == 1
    assert stored["zones"][0]["user_edited"]


def test_a_confirmed_mask_can_be_saved_and_reused(client, tmp_path):
    _seed(tmp_path)
    template_id = client.get("/api/mask/templates").json()["templates"][0]["template_id"]

    client.put(
        f"/api/mask/{template_id}",
        json={
            "zones": [
                {
                    "type": "titleblock",
                    "rect": {"x0": 0.7, "y0": 0.85, "x1": 1.0, "y1": 1.0},
                    "label": "Title block",
                }
            ],
            "profile_id": "house-style",
        },
    )

    loaded = client.post(f"/api/mask/{template_id}/load/house-style").json()
    assert len(loaded["zones"]) == 1
    assert loaded["confirmed"]


def test_a_template_applies_to_another_sheet_in_its_cluster(client, tmp_path):
    _seed(tmp_path)
    payload = client.get("/api/mask/templates").json()["templates"][0]
    other = next(
        sheet_id
        for sheet_id in payload["member_sheet_ids"]
        if sheet_id != payload["representative_sheet_id"]
    )

    preview = client.get(f"/api/mask/{payload['template_id']}/preview/{other}").json()

    assert preview["sheet_id"] == other
    assert len(preview["zones"]) == len(payload["zones"])


def test_an_unknown_template_is_a_clean_404(client, tmp_path):
    _seed(tmp_path)
    client.get("/api/mask/templates")
    response = client.get("/api/mask/template-nonsense")

    assert response.status_code == 404
    assert "no longer in this comparison" in response.json()["error"]["message"]


def test_an_unknown_zone_type_is_refused_with_the_allowed_values(client, tmp_path):
    _seed(tmp_path)
    template_id = client.get("/api/mask/templates").json()["templates"][0]["template_id"]

    response = client.put(
        f"/api/mask/{template_id}",
        json={"zones": [{"type": "nonsense", "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}]},
    )

    assert response.status_code == 422
    assert "titleblock" in response.json()["error"]["message"]


def test_templates_need_readable_sheets(client):
    reset_session()
    response = client.get("/api/mask/templates")

    assert response.status_code == 422
    assert "no readable sheets" in response.json()["error"]["message"]
