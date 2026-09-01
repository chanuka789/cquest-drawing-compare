"""API surface: health endpoint and the structured error shape."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.api.app import create_app
from engine.storage.schema import SCHEMA_VERSION
from engine.utils.errors import AlignmentFailedError, NotFoundError


def test_health_reports_status_and_paths(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "C-Quest Drawing Compare"
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["ai_mode"] == "offline"
    assert set(body["paths"]) == {"root", "db", "logs", "cache", "profiles"}
    assert body["paths"]["logs"].endswith("logs")


def test_unknown_api_route_returns_structured_error(client: TestClient):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404

    error = response.json()["error"]
    assert error["code"] == "http_404"
    assert isinstance(error["message"], str)


def test_app_error_becomes_structured_json():
    app: FastAPI = create_app()

    @app.get("/api/_test/missing")
    def _missing() -> None:
        raise NotFoundError("That project was moved or deleted. Open a different one.")

    with TestClient(app) as client:
        response = client.get("/api/_test/missing")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"].startswith("That project was moved")


def test_error_detail_is_carried_through():
    app: FastAPI = create_app()

    @app.get("/api/_test/alignment")
    def _alignment() -> None:
        raise AlignmentFailedError(detail={"rms_error": 42.5, "sheet": "A-101"})

    with TestClient(app) as client:
        response = client.get("/api/_test/alignment")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "alignment_failed"
    assert error["detail"]["rms_error"] == 42.5


def test_cors_is_enabled_only_in_dev_mode(monkeypatch, tmp_path):
    from engine.settings import Settings

    dev = create_app(Settings(dev_mode=True, log_level="INFO"))
    prod = create_app(Settings(dev_mode=False, log_level="INFO"))

    origin = {"Origin": "http://localhost:5173"}

    with TestClient(dev) as client:
        dev_response = client.get("/api/health", headers=origin)
    with TestClient(prod) as client:
        prod_response = client.get("/api/health", headers=origin)

    assert dev_response.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in prod_response.headers


def test_docs_are_hidden_in_production():
    from engine.settings import Settings

    prod = create_app(Settings(dev_mode=False, log_level="INFO"))
    with TestClient(prod) as client:
        assert client.get("/api/openapi.json").status_code == 404
