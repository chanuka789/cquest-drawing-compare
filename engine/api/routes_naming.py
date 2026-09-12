"""Phase 3 routes: sheet matching review and safe bulk renaming.

Thin by design, like every router in this application: the routes validate
input, call a session method or a naming module, and return JSON. Long file
work (applying renames, undoing them, matching runs that must fingerprint
hundreds of sheets) runs on the session's worker threads with progress
reported through :class:`ComparisonSession` state, which the UI polls.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.session import get_session
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["matching", "rename"])


# ── Request models ──────────────────────────────────────────────────────


class ManualPair(BaseModel):
    old_key: str = Field(min_length=1)
    new_key: str = Field(min_length=1)


class MatchDecisionsRequest(BaseModel):
    #: old-sheet keys of review pairs the user accepted or rejected.
    accepted: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    #: manual or alternative pairings of old-unmatched -> new-unmatched keys.
    manual: list[ManualPair] = Field(default_factory=list)


class RenamePlanRequest(BaseModel):
    template: str = Field(min_length=1)
    mode: str = "copy"
    use_discipline_folders: bool = False
    #: source path -> replacement file name, chosen row by row in the UI.
    overrides: dict[str, str] = Field(default_factory=dict)


class RenameApplyRequest(BaseModel):
    i_understand: bool = False


# ── Matching review ─────────────────────────────────────────────────────


@router.get("/match/status", summary="Where the matching run has got to")
def match_status() -> dict[str, Any]:
    return get_session().matching_status()


@router.post("/match/run", summary="Match the two issue sets in the background")
def run_match() -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_matching()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}


@router.get("/match/result", summary="The match result, once the run is ready")
def match_result() -> dict[str, Any]:
    from engine.naming.matcher import MatchResult, result_as_dict

    session = get_session()
    if session.matching["state"] != "ready" or not isinstance(session.match_result, MatchResult):
        raise ValidationError("Matching has not finished yet. Check its status first.")
    return result_as_dict(session.match_result)


@router.post("/match/decisions", summary="Record the user's review decisions")
def save_decisions(body: MatchDecisionsRequest) -> dict[str, int]:
    session = get_session()
    return session.apply_match_decisions(
        accepted=body.accepted,
        rejected=body.rejected,
        manual=[item.model_dump() for item in body.manual],
    )


@router.post("/match/finalize", summary="Write the review snapshot into the audit folder")
def finalize_match() -> dict[str, str | None]:
    session = get_session()
    try:
        path = session.finalize_matching()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"path": path}


# ── Rename: templates and context ───────────────────────────────────────


@router.get("/rename/templates", summary="The naming template presets")
def naming_templates() -> list[dict[str, str]]:
    from engine.naming.template import available_templates

    return [template.as_dict() for template in available_templates()]


@router.get("/rename/profile-context", summary="Fixed template tokens from the sheet profile")
def profile_context() -> dict[str, str]:
    return get_session().rename_context()


# ── Rename: plan, apply, status, cancel, undo ───────────────────────────


def _action_as_dict(action: Any) -> dict[str, Any]:
    return {
        "source_path": getattr(action, "source_path", ""),
        "target_path": getattr(action, "target_path", ""),
        "target_folder": getattr(action, "target_folder", ""),
        "status": str(getattr(action, "status", "ok")),
        "old_name": getattr(action, "old_name", ""),
        "new_name": getattr(action, "new_name", ""),
        "warnings": list(getattr(action, "warnings", [])),
    }


def _plan_as_dict(plan: Any) -> dict[str, Any]:
    summary = getattr(plan, "summary", None)
    if not isinstance(summary, dict):
        actions = list(getattr(plan, "actions", []) or [])
        counts: dict[str, int] = {}
        for action in actions:
            status = str(getattr(action, "status", "ok"))
            counts[status] = counts.get(status, 0) + 1
        counts.setdefault("to_change", counts.get("ok", 0))
        summary = counts
    return {
        "actions": [_action_as_dict(action) for action in (getattr(plan, "actions", []) or [])],
        "summary": summary,
        "can_apply": bool(getattr(plan, "can_apply", False)),
        "errors": list(getattr(plan, "errors", []) or []),
        "mode": str(getattr(plan, "mode", "copy")),
        "output_dir": str(getattr(plan, "output_dir", "")),
    }


@router.post("/rename/plan", summary="Dry-run the rename and show the full plan")
def build_rename_plan(body: RenamePlanRequest) -> dict[str, Any]:
    from engine.naming.template import preview

    session = get_session()
    if not session.new.sheets:
        raise ValidationError("There are no drawings on the current issue to rename.")

    try:
        plan = session.plan_renames(
            body.template,
            mode=body.mode,
            use_discipline_folders=body.use_discipline_folders,
            overrides=body.overrides,
        )
    except (ValueError, OSError) as exc:
        raise ValidationError(str(exc)) from exc

    result = _plan_as_dict(plan)
    result["preview"] = preview(body.template, list(session.new.sheets), session.rename_context())
    return result


@router.post("/rename/apply", summary="Apply the last plan, copying by default")
def apply_rename(body: RenameApplyRequest) -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_renames(i_understand=body.i_understand)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}


@router.get("/rename/status", summary="Progress of the running rename or undo")
def rename_status() -> dict[str, Any]:
    return get_session().rename_status()


@router.post("/rename/cancel", summary="Stop the running rename cleanly")
def cancel_rename() -> dict[str, bool]:
    return {"cancelled": get_session().cancel_rename()}


@router.post("/rename/undo", summary="Reverse the last rename, verifying hashes")
def undo_rename() -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_undo()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}
