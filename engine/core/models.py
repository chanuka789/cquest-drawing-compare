"""Pydantic models for anything that crosses the API boundary.

Keep these in step with `ui/src/api/types.ts`. If a field changes here, it
changes there in the same commit.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.core.enums import AiMode


class AppPathsInfo(BaseModel):
    """Where the application keeps its own data on this machine."""

    root: str
    db: str
    logs: str
    cache: str
    profiles: str


class HealthResponse(BaseModel):
    """Returned by `GET /api/health`. The UI calls this before anything else."""

    status: str = Field(default="ok", description="Always 'ok' when the engine answers.")
    app_name: str
    version: str
    dev_mode: bool
    ai_mode: AiMode = Field(
        default=AiMode.OFFLINE,
        description="Offline is the default. AI never turns itself on.",
    )
    schema_version: int
    paths: AppPathsInfo


class ErrorBody(BaseModel):
    """The inner object of a structured error response."""

    code: str
    message: str
    detail: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Every failed request returns this shape."""

    error: ErrorBody
