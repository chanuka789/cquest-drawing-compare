"""System routes: health and version.

Thin by design. The shell polls `/api/health` before it opens the window, and
the UI calls it once on start to prove the engine is reachable.
"""

from __future__ import annotations

from fastapi import APIRouter

from engine.core.enums import AiMode
from engine.core.models import AppPathsInfo, HealthResponse
from engine.settings import get_settings
from engine.storage.paths import get_app_paths
from engine.storage.schema import SCHEMA_VERSION

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Is the engine running")
def health() -> HealthResponse:
    """Report engine status, version, mode and where its data lives."""
    settings = get_settings()
    paths = get_app_paths()

    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        version=settings.version,
        dev_mode=settings.dev_mode,
        # Phase 1 has no settings screen yet, so the app is always offline.
        ai_mode=AiMode.OFFLINE,
        schema_version=SCHEMA_VERSION,
        paths=AppPathsInfo(**paths.as_dict()),
    )
