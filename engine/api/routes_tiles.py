"""Phase 4, task 4.3: tile serving endpoints and the pre-render queue.

Thin by design, like every router here: the routes resolve the session's
sheet index and the workspace tiles root, then delegate all rendering,
caching and queueing to :class:`engine.extract.tile_service.TileService`
(created once per session and attached to it, so the queue state and the
in-memory level cache outlive individual requests).

There is deliberately no access control on these tiles: this desktop engine
serves its own single window over loopback, and only it knows the sheet ids.

Literal route segments are registered before the parameterised tile route,
per the repository rule - even though the different segment counts would
prevent a swallow here, staying in the habit keeps the next route from being
the one that bites.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from engine.core.session import ComparisonSession, get_session
from engine.extract.tile_service import TileService
from engine.extract.tiler import tiles_root
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["tiles"])

#: Comparison DPI for tiles (the Phase 4 default; hard-coded nowhere else).
TILE_DPI = 200

#: Priority levels the UI may send. All currently run FIFO - see the
#: TileService docstring for why the distinction is not implemented yet.
_ALLOWED_PRIORITIES: frozenset[str] = frozenset({"immediate", "high", "normal", "low"})

#: Tiles are immutable for a given sheet id, so browsers may cache forever.
_PNG_CACHE_HEADERS: dict[str, str] = {"Cache-Control": "public, max-age=31536000, immutable"}


# ── Helpers ────────────────────────────────────────────────────────────────


def _sheet_id(abs_path: str, page_index: int) -> str:
    """The stable id of one sheet: sha1 of the path and page, 16 hex chars.

    Two revisions of the same file may share a path on different pages, so
    the page is part of the id. Content hashing is unnecessary for id
    uniqueness (the id only names a tile pyramid on disk).
    """
    digest = hashlib.sha1(f"{abs_path}|{page_index}".encode())
    return digest.hexdigest()[:16]


def sheet_id(abs_path: str, page_index: int) -> str:
    """Public form of :func:`_sheet_id`, for sibling routers and the UI."""
    return _sheet_id(abs_path, page_index)


def _sheet_index(session: ComparisonSession) -> dict[str, tuple[str, int]] | None:
    """``{sheet_id: (abs_path, page_index)}`` over both sides of *session*.

    Built lazily and cached on the session (``session._tile_index``) because
    every tile request would otherwise re-hash every sheet; the cache is
    rebuilt whenever the sheet count on either side changes, and is None when
    the session has no sheets at all.
    """
    stamp = (len(session.old.sheets), len(session.new.sheets))
    cached = getattr(session, "_tile_index", None)
    if cached is not None and getattr(session, "_tile_index_stamp", None) == stamp:
        return cached

    index: dict[str, tuple[str, int]] = {}
    for sheet in [*session.old.sheets, *session.new.sheets]:
        sheet_id = _sheet_id(sheet.abs_path, sheet.page_index)
        index[sheet_id] = (sheet.abs_path, sheet.page_index)

    result: dict[str, tuple[str, int]] | None = index if index else None
    session._tile_index = result  # type: ignore[attr-defined]
    session._tile_index_stamp = stamp  # type: ignore[attr-defined]
    return result


def _tiles_dir(session: ComparisonSession) -> Path:
    """The workspace tiles root (``<workspace>/_audit/tiles``).

    Tiles must travel with the comparison, so there is no per-session temp
    fallback. There is, however, no reason to make the user answer the
    output-folder question before they may look at a drawing: the
    suggested folder is derived from the inputs and created on demand.
    Refusing here is what used to leave the viewer silently blank.
    """
    workspace = session.workspace
    if workspace is None:
        try:
            workspace = session.ensure_workspace()
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return tiles_root(workspace)


def _service(session: ComparisonSession) -> TileService:
    """The session's TileService, created once and attached to the session."""
    service = getattr(session, "_tile_service", None)
    if service is None:
        service = TileService(session, dpi=TILE_DPI, sheet_index=_sheet_index)
        session._tile_service = service  # type: ignore[attr-defined]
    return service


def _tile_response(data: bytes) -> StreamingResponse:
    return StreamingResponse(
        iter([data]),
        media_type="image/png",
        headers=_PNG_CACHE_HEADERS,
    )


# ── Request bodies ─────────────────────────────────────────────────────────


class RenderQueueRequest(BaseModel):
    sheet_ids: list[str] = Field(default_factory=list)
    priority: str = "normal"


# ── Tile endpoints ─────────────────────────────────────────────────────────


# Literal second segments first, then the parameterised tile route.
@router.get("/tiles/{sheet_id}/manifest", summary="The pyramid manifest of one sheet")
def sheet_manifest(sheet_id: str) -> dict[str, object]:
    """Render geometry for the viewer: dimensions, DPI and level grid."""
    session = get_session()
    _tiles_dir(session)
    return _service(session).ensure_manifest(sheet_id)


@router.get("/tiles/{sheet_id}/thumbnail.png", summary="The level-0 thumbnail of one sheet")
def sheet_thumbnail(sheet_id: str) -> StreamingResponse:
    session = get_session()
    _tiles_dir(session)
    return _tile_response(_service(session).get_thumbnail(sheet_id))


@router.get(
    "/tiles/{sheet_id}/{level}/{x}/{y}.png",
    summary="One PNG tile of a sheet pyramid, generated on demand",
)
def sheet_tile(sheet_id: str, level: int, x: int, y: int) -> StreamingResponse:
    session = get_session()
    _tiles_dir(session)
    data = _service(session).get_tile(sheet_id, level, x, y)
    return _tile_response(data)


# ── The pre-render queue ───────────────────────────────────────────────────


@router.post("/render/queue", summary="Pre-render whole pyramids in the background")
def queue_renders(body: RenderQueueRequest) -> dict[str, int]:
    session = get_session()
    _tiles_dir(session)
    if body.priority not in _ALLOWED_PRIORITIES:
        raise ValidationError(
            "Priority must be one of: immediate, high, normal or low.",
            detail={"priority": body.priority},
        )
    index = _sheet_index(session)
    unknown = [sheet_id for sheet_id in body.sheet_ids if index is None or sheet_id not in index]
    if unknown:
        raise ValidationError(
            "Some of those sheets are not part of this comparison.",
            detail={"sheet_ids": unknown},
        )
    return {"queued": _service(session).enqueue(body.sheet_ids, priority=body.priority)}


@router.get("/render/status", summary="State of the pre-render queue")
def render_status() -> dict[str, object]:
    service = getattr(get_session(), "_tile_service", None)
    if service is None:
        return {"queued": 0, "done": 0, "failed": 0, "running": False, "last_message": ""}
    return service.status()
