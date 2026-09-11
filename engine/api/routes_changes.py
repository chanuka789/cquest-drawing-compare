"""Task 5.15 (API) — queueing comparisons and reading their results.

The route order here matters and is not cosmetic. FastAPI matches in
declaration order, so `/api/compare/status` **must** be declared before
`/api/compare/{pair_id}/stats`-style parameterised paths, or the literal path
is swallowed by the parameter and `status` arrives as a pair id. Phase 2 was
bitten by exactly this with `/api/scan/cancel`.

The routers stay thin, as the project rules require: validate, call a service,
return. Everything interesting lives in :mod:`engine.compare.orchestrator` and
:mod:`engine.storage.change_store`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.enums import UserStatus
from engine.core.session import get_session
from engine.utils.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/api", tags=["comparison"])


class QueueRequest(BaseModel):
    """Which pairs to compare. Empty means every aligned pair."""

    pair_ids: list[str] = Field(default_factory=list)


class StatusUpdate(BaseModel):
    """The triage state a user set on one change."""

    user_status: str

    def validated(self) -> str:
        try:
            return str(UserStatus(self.user_status))
        except ValueError as exc:
            allowed = ", ".join(str(value) for value in UserStatus)
            raise ValidationError(
                f"'{self.user_status}' is not a status. Use one of: {allowed}."
            ) from exc


# ── Literal paths first ─────────────────────────────────────────────────


@router.post("/compare/queue", summary="Queue the aligned pairs for comparison")
def queue_comparison(body: QueueRequest | None = None) -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_comparisons(body.pair_ids if body else None)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}


@router.get("/compare/status", summary="Progress of the comparison run")
def comparison_status() -> dict[str, Any]:
    return get_session().compare_status()


@router.post("/compare/cancel", summary="Stop the comparison run cleanly")
def cancel_comparison() -> dict[str, bool]:
    return {"cancelled": get_session().cancel_comparison()}


@router.get("/compare/results", summary="Every compared pair, in summary")
def comparison_results() -> dict[str, Any]:
    session = get_session()
    rows = [
        result.as_dict()  # type: ignore[attr-defined]
        for result in session.compare_results.values()
    ]
    rows.sort(key=lambda row: str(row.get("pair_id", "")))
    return {"results": rows, "summary": dict(session.compare_run["summary"])}


# ── Parameterised paths after ───────────────────────────────────────────


def _result_for(pair_id: str) -> Any:
    result = get_session().compare_results.get(pair_id)
    if result is None:
        raise NotFoundError("That pair has not been compared yet. Run the comparison first.")
    return result


@router.get("/changes/{pair_id}", summary="The changes found on one pair")
def changes_for_pair(
    pair_id: str,
    stream: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    include_cosmetic: bool = False,
) -> dict[str, Any]:
    """Changes for one pair, filtered.

    Cosmetic changes are hidden unless asked for. A report that shows twelve
    changes and can produce the four hundred it suppressed is trustworthy;
    one that shows four hundred and twelve is not.
    """
    result = _result_for(pair_id)
    changes = result.changes if include_cosmetic else result.reportable

    if stream:
        changes = [change for change in changes if stream in {str(item) for item in change.streams}]
    if kind:
        changes = [change for change in changes if str(change.kind) == kind]
    if category:
        changes = [change for change in changes if change.category == category]

    tolerance = result.tolerance
    return {
        "pair_id": pair_id,
        "changes": [_change_payload(change, tolerance) for change in changes],
        "warnings": [warning.as_dict() for warning in result.warnings],
        "tolerance": tolerance.as_dict() if tolerance else None,
        "revision_rows": [row.as_dict() for row in result.revision_rows],
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
    }


@router.get("/changes/{pair_id}/filtered", summary="What was suppressed, and why")
def filtered_for_pair(pair_id: str) -> dict[str, Any]:
    """The debug view. Nothing is ever discarded silently."""
    result = _result_for(pair_id)
    tolerance = result.tolerance
    return {
        "pair_id": pair_id,
        "filtered": [
            {
                "filter": entry.filter_name,
                "reason": entry.reason,
                "change": _change_payload(entry.change, tolerance),
            }
            for entry in result.filtered
        ],
    }


@router.patch("/changes/{pair_id}/{change_index}", summary="Set a change's status")
def set_change_status(pair_id: str, change_index: int, body: StatusUpdate) -> dict[str, Any]:
    result = _result_for(pair_id)
    if not 0 <= change_index < len(result.changes):
        raise NotFoundError("That change is no longer in this comparison.")
    status = body.validated()
    change = result.changes[change_index]
    change.detail["user_status"] = status
    return {"pair_id": pair_id, "change_index": change_index, "user_status": status}


@router.get("/compare/{pair_id}/stats", summary="Per-stream statistics for one pair")
def comparison_stats(pair_id: str) -> dict[str, Any]:
    result = _result_for(pair_id)
    by_kind: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for change in result.changes:
        by_kind[str(change.kind)] = by_kind.get(str(change.kind), 0) + 1
        by_category[change.category] = by_category.get(change.category, 0) + 1

    by_filter: dict[str, int] = {}
    for entry in result.filtered:
        by_filter[entry.filter_name] = by_filter.get(entry.filter_name, 0) + 1

    return {
        "pair_id": pair_id,
        "total": len(result.changes),
        "reportable": len(result.reportable),
        "cosmetic": len(result.cosmetic),
        "filtered": len(result.filtered),
        "by_kind": by_kind,
        "by_category": by_category,
        "by_filter": by_filter,
        "streams": [stat.as_dict() for stat in result.stream_stats],
        "timings": {name: round(value, 3) for name, value in result.timings.items()},
        "engine_version": result.engine_version,
        "config": result.config_snapshot,
    }


def _change_payload(change: Any, tolerance: Any) -> dict[str, Any]:
    """One change, in millimetres. A pixel count never reaches a user."""
    box = change.bbox
    if tolerance is not None and tolerance.px_per_mm > 0:
        x, y, width, height = box.to_mm(tolerance.px_per_mm)
    else:
        x, y, width, height = box.x, box.y, box.w, box.h

    payload: dict[str, Any] = {
        "kind": str(change.kind),
        "type": change.kind.coarse(),
        "streams": [str(stream) for stream in change.streams],
        "category": change.category,
        "description": change.description,
        "confidence": round(change.confidence, 3),
        "is_cosmetic": change.is_cosmetic,
        "bbox_mm": {
            "x": round(x, 2),
            "y": round(y, 2),
            "w": round(width, 2),
            "h": round(height, 2),
        },
        "user_status": change.detail.get("user_status", "open"),
    }
    if change.text is not None:
        payload["text"] = {
            "category": str(change.text.category),
            "old_text": change.text.old_text,
            "new_text": change.text.new_text,
            "numeric_delta": change.text.numeric_delta,
            "percent_delta": change.text.percent_delta,
            "distance_moved_mm": change.text.distance_moved_site_mm,
            "cross_check": str(change.text.cross_check),
        }
    if change.hatch is not None:
        payload["hatch"] = {
            "old_signature": change.hatch.old_signature,
            "new_signature": change.hatch.new_signature,
            "area_delta_m2": change.hatch.area_delta_m2,
            "segment_count": change.hatch.segment_count,
        }
    if change.vector is not None:
        payload["vector"] = {
            "geometry_type": change.vector.geometry_type,
            "path_count": change.vector.path_count,
            "length_site_mm": change.vector.total_length_site_mm,
            "style_difference": change.vector.style_difference,
        }
    return payload
