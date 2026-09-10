"""FastAPI application factory.

Two modes, both built now so packaging in Phase 8 holds no surprises:

* Development (`CQDC_DEV=1`) — the UI is served by Vite on port 5173, so the
  browser origin differs from the API origin and CORS is required.
* Production — the built UI in `ui/dist` is served by this application at `/`,
  same origin, no CORS.

Every deliberate failure leaves the engine as an :class:`AppError` and is
converted here into `{"error": {"code", "message", "detail"}}`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from engine.api import (
    routes_align,
    routes_changes,
    routes_ingest,
    routes_naming,
    routes_register,
    routes_report,
    routes_system,
    routes_tiles,
    ws_progress,
)
from engine.core.events import shutdown_requested
from engine.core.project import ensure_workspace_db
from engine.settings import Settings, get_settings
from engine.storage.paths import bundle_root, get_app_paths
from engine.utils.errors import AppError
from engine.utils.logging_setup import is_configured, setup_logging


def ui_dist_dir() -> Path:
    """Directory holding the built frontend, if this build has one."""
    return bundle_root() / "ui" / "dist"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    paths = get_app_paths()
    logger.info(
        "Engine starting | version={} | dev_mode={} | data={}",
        settings.version,
        settings.dev_mode,
        paths.root,
    )

    # Prove the storage layer on the real machine, not only in the tests.
    engine = ensure_workspace_db()
    app.state.db = engine

    yield

    # Tell long-lived readers to let go, so shutdown is prompt.
    shutdown_requested.set()
    engine.dispose()
    logger.info("Engine stopped")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError | code={} | {}", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Some of the values sent were not valid.",
                    "detail": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                    "detail": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Anything reaching here is a bug. Log it fully, tell the user where
        # to look, and never leak the traceback into the response.
        logger.exception("Unhandled error: {}", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": (
                        "Something went wrong inside the application. "
                        "The details are in the log file."
                    ),
                    "detail": {"log_dir": str(get_app_paths().logs)},
                }
            },
        )


def _mount_ui(app: FastAPI) -> None:
    """Serve the built UI at `/` with an SPA fallback.

    Without the fallback, refreshing the page on any route other than `/`
    returns 404 because the file does not exist on disk.
    """
    dist = ui_dist_dir()
    index = dist / "index.html"

    if not index.exists():
        logger.warning(
            "No built UI found at {} - the engine will answer API calls only. "
            "Run 'npm run build' in ui/ to produce it.",
            dist,
        )
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # An unknown API path must still fail as JSON. Without this, every
        # mistyped endpoint would return the HTML page with status 200 and
        # the frontend would try to parse a document as a response.
        if full_path.startswith("api/"):
            raise StarletteHTTPException(status_code=404, detail="Not Found")

        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)

    logger.info("Serving built UI from {}", dist)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Call this rather than importing a global."""
    settings = settings or get_settings()
    shutdown_requested.clear()

    if not is_configured():
        setup_logging()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        summary="Local engine for construction drawing set reconciliation.",
        lifespan=_lifespan,
        docs_url="/api/docs" if settings.dev_mode else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.dev_mode else None,
    )

    if settings.dev_mode:
        # The Vite dev server only. Never a wildcard - this process can read
        # the user's drawings and must not be reachable from any web page.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.dev_server_url, "http://127.0.0.1:5173"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _register_error_handlers(app)
    app.include_router(routes_system.router)
    app.include_router(routes_ingest.router)
    app.include_router(routes_register.router)
    app.include_router(routes_report.router)
    app.include_router(routes_naming.router)
    app.include_router(routes_align.router)
    app.include_router(routes_changes.router)
    app.include_router(routes_tiles.router)
    app.include_router(ws_progress.router)

    # The SPA fallback claims `/{path}`, so it must be mounted last.
    if not settings.dev_mode:
        _mount_ui(app)

    return app


#: Module-level instance so `uvicorn engine.api.app:app` works during development.
app = create_app()
