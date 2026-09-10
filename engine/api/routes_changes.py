"""Phase 5 routes: the change list for the compared pairs.

Thin, like every router here: the routes validate, call the session, and
serialise. All the detection lives in :mod:`engine.compare`.

Two entry points, because comparing is a tool in its own right and not a
step in a wizard. ``/compare/start`` takes a user from two chosen folders
to a change list without asking them to walk the matching review first;
``/compare/run`` is the same batch for someone who already aligned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.session import get_session
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["changes"])


class ComparePairRequest(BaseModel):
    """One pair, compared on its own — the single-sheet tool."""

    old_sheet_id: str = Field(min_length=1)
    new_sheet_id: str = Field(min_length=1)
    #: Reuse the alignment already fitted for this batch row, when there is one.
    align_index: int | None = None
    mask_title_block: bool = True


def _region_row(region: Any) -> dict[str, Any]:
    """One change, in the units the user reads."""
    return {
        "index": region.index,
        "type": str(region.change_type),
        "severity": str(region.severity),
        "bbox_px": list(region.bbox_px),
        "bbox_mm": [round(value, 2) for value in region.bbox_mm],
        "area_mm2": round(region.area_mm2, 2),
        "area_site_mm2": (
            round(region.area_site_mm2, 2) if region.area_site_mm2 is not None else None
        ),
        "added_px": region.added_px,
        "removed_px": region.removed_px,
        "is_cosmetic": region.is_cosmetic,
        "moved_from_px": list(region.moved_from_px) if region.moved_from_px else None,
        "moved_by_mm": (round(region.moved_by_mm, 2) if region.moved_by_mm is not None else None),
        "moved_by_site_mm": (
            round(region.moved_by_site_mm, 2) if region.moved_by_site_mm is not None else None
        ),
        "text_kind": region.text_kind,
        "old_text": region.old_text,
        "new_text": region.new_text,
        "explanation": region.explanation,
    }


def _result_row(item: Any) -> dict[str, Any]:
    """One compared sheet and everything found on it."""
    from engine.api.routes_tiles import sheet_id as tile_sheet_id

    return {
        "old": {
            "filename": Path(item.old_path).name,
            "sheet_id": tile_sheet_id(item.old_path, item.old_page_index),
        },
        "new": {
            "filename": Path(item.new_path).name,
            "sheet_id": tile_sheet_id(item.new_path, item.new_page_index),
        },
        "width_px": item.width_px,
        "height_px": item.height_px,
        "dpi": item.dpi,
        "scale_denominator": item.scale_denominator,
        "counts": item.counts,
        "region_count": len(item.regions),
        "substantive_count": item.substantive_count,
        "truncated": item.truncated,
        "duration_s": round(item.duration_s, 3),
        "failure": item.failure,
        "regions": [_region_row(region) for region in item.regions],
    }


@router.post("/compare/start", summary="Match, align and compare in one go")
def start_compare() -> dict[str, Any]:
    """The standalone compare tool.

    Everything comparing needs — a workspace to write into, a set of pairs,
    and a transform per pair — is arranged here rather than being demanded
    of the user as a sequence of screens. Matching and alignment still run,
    and their review screens are still there; they are simply no longer a
    toll gate in front of the thing the user actually asked for.
    """
    session = get_session()
    try:
        session.ensure_workspace()
        session.ensure_matched()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    if session.align_run["state"] == "running":
        raise ValidationError("Alignment is already running. Wait for it to finish.")

    # Align first if nothing has been aligned for these pairs yet.
    if not session.align_results:
        try:
            session.run_alignments()
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return {"stage": "aligning", "run_id": session.align_run["run_id"]}

    try:
        run_id = session.run_comparisons()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"stage": "comparing", "run_id": run_id}


@router.post("/compare/run", summary="Compare the aligned pairs in the background")
def run_compare() -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_comparisons()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}


@router.get("/compare/status", summary="Progress of the comparison run")
def compare_status() -> dict[str, Any]:
    return get_session().compare_status()


@router.post("/compare/cancel", summary="Stop the comparison run cleanly")
def cancel_compare() -> dict[str, bool]:
    return {"cancelled": get_session().cancel_compare()}


@router.get("/compare/results", summary="The change list once the run is done")
def compare_results() -> dict[str, Any]:
    session = get_session()
    state = str(session.compare_run["state"])
    if state not in {"done", "cancelled"}:
        raise ValidationError("The comparison has not finished yet. Check its status first.")
    return {
        "results": [_result_row(item) for item in session.compare_results],
        "summary": dict(session.compare_run["summary"]),
    }


@router.post("/compare/pair", summary="Compare one pair on its own")
def compare_one_pair(body: ComparePairRequest) -> dict[str, Any]:
    """Compare a single chosen pair, without running the whole batch.

    Runs synchronously: it is one user action on one pair, and the wait is
    a couple of seconds.
    """
    import numpy as np

    from engine.api.routes_tiles import _sheet_index
    from engine.compare.pipeline import compare_pair
    from engine.compare.types import CompareConfig

    session = get_session()
    try:
        session.ensure_workspace()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    index_map = _sheet_index(session)
    if not index_map:
        raise ValidationError("There are no scanned sheets to compare.")
    old_spec = index_map.get(body.old_sheet_id)
    new_spec = index_map.get(body.new_sheet_id)
    if old_spec is None or new_spec is None:
        raise ValidationError("One of those two sheets is not part of this comparison.")

    matrix = None
    if body.align_index is not None:
        if not 0 <= body.align_index < len(session.align_results):
            raise ValidationError("That alignment result no longer exists.")
        raw = getattr(session.align_results[body.align_index], "matrix", None)
        if raw is not None:
            matrix = np.asarray(raw, dtype=float)

    denominator = None
    for state in (session.old, session.new):
        for sheet in state.sheets:
            if sheet.abs_path == new_spec[0] and sheet.page_index == new_spec[1]:
                from engine.align.orchestrator import _scale_denominator

                denominator = _scale_denominator(getattr(sheet, "scale", None))

    result = compare_pair(
        old_spec[0],
        new_spec[0],
        old_page_index=old_spec[1],
        new_page_index=new_spec[1],
        matrix=matrix,
        config=CompareConfig(),
        scale_denominator=denominator,
        mask_title_block=body.mask_title_block,
    )
    return _result_row(result)
