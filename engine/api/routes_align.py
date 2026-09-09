"""Phase 4 routes: batch alignment of the accepted match pairs.

The heavy work runs on the session's worker thread with progress exposed as
polled state (matching the rename pattern); results are snapshotted into the
workspace audit folder when the run completes. Manual alignment of a single
pair runs synchronously (it is one user action on one pair).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.session import get_session
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["alignment"])


class ManualPoint(BaseModel):
    old_x: float
    old_y: float
    new_x: float
    new_y: float


class ManualAlignRequest(BaseModel):
    old_sheet_id: str = Field(min_length=1)
    new_sheet_id: str = Field(min_length=1)
    points: list[ManualPoint] = Field(min_length=2)
    #: Which result row this replaces (when the batch run has finished).
    index: int | None = None


@router.post("/align/run", summary="Align the accepted pairs in the background")
def run_alignment() -> dict[str, str]:
    session = get_session()
    try:
        run_id = session.run_alignments()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"run_id": run_id}


@router.get("/align/status", summary="Progress of the alignment run")
def alignment_status() -> dict[str, Any]:
    return get_session().align_status()


@router.post("/align/cancel", summary="Stop the alignment run cleanly")
def cancel_alignment() -> dict[str, bool]:
    return {"cancelled": get_session().cancel_alignment()}


def _matrix_to_list(matrix: Any) -> list[list[float]] | None:
    if matrix is None:
        return None
    try:
        return [[float(value) for value in row] for row in matrix]
    except (TypeError, ValueError):
        return None


def _row_for(index: int, item: Any, refs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """One serialised result row, shared by the batch and manual endpoints."""
    from pathlib import Path

    from engine.api.routes_tiles import sheet_id as tile_sheet_id

    assessment = getattr(item, "assessment", None)
    old_ref = refs[index][0] if index < len(refs) else None
    new_ref = refs[index][1] if index < len(refs) else None
    old_path = getattr(old_ref, "abs_path", "")
    new_path = getattr(new_ref, "abs_path", "")
    return {
        "old": {
            "filename": Path(old_path).name,
            "sheet_id": tile_sheet_id(old_path, int(getattr(old_ref, "page_index", 0)))
            if old_path
            else None,
        },
        "new": {
            "filename": Path(new_path).name,
            "sheet_id": tile_sheet_id(new_path, int(getattr(new_ref, "page_index", 0)))
            if new_path
            else None,
        },
        "verdict": str(getattr(item, "verdict", "failed")),
        "method": str(getattr(item, "method", "")),
        "note": getattr(item, "note", ""),
        "duration_s": round(float(getattr(item, "duration_s", 0.0)), 3),
        "matrix": _matrix_to_list(getattr(item, "matrix", None)),
        "explanation": getattr(assessment, "explanation", "")
        if assessment is not None
        else "",
        "rms_mm_on_paper": getattr(assessment, "rms_mm_on_paper", None)
        if assessment is not None
        else None,
        "rms_mm_on_site": getattr(assessment, "rms_mm_on_site", None)
        if assessment is not None
        else None,
        "metrics": _metrics_to_dict(getattr(assessment, "metrics", None)),
    }


def _metrics_to_dict(metrics: Any) -> dict[str, dict[str, float | bool]] | None:
    """The seven quality metrics: value, threshold and pass flag per metric."""
    if not isinstance(metrics, dict):
        return None
    output: dict[str, dict[str, float | bool]] = {}
    for name, (value, threshold, passed) in metrics.items():
        output[str(name)] = {"value": float(value), "threshold": float(threshold), "passed": bool(passed)}
    return output


@router.get("/align/results", summary="The alignment results once the run is done")
def alignment_results() -> dict[str, Any]:
    session = get_session()
    if session.align_run["state"] != "done":
        raise ValidationError("Alignment has not finished yet. Check its status first.")
    refs = list(session._align_pair_refs)
    rows = [_row_for(index, item, refs) for index, item in enumerate(session.align_results)]
    return {"results": rows, "summary": dict(session.align_run["summary"])}


@router.post("/align/manual", summary="Fit a transform from user-clicked points")
def manual_align(body: ManualAlignRequest) -> dict[str, Any]:
    """Manual alignment for one pair.

    The user clicked matching points on the two sheets (two points give an
    exact similarity; three or more a least-squares fit with a residual).
    The result runs the same quality assessment as the automatic path and
    replaces the batch row at `index` when one is given.
    """
    from engine.align.orchestrator import apply_manual
    from engine.align.units import px_per_mm as ppm
    from engine.api.routes_tiles import _sheet_index
    from engine.extract.raster_renderer import RenderOptions, render_page

    session = get_session()
    index_map = _sheet_index(session)
    if not index_map:
        raise ValidationError("There are no scanned sheets to align.")
    lookup = {sheet_id: (path, page) for sheet_id, (path, page) in index_map.items()}
    old_spec = lookup.get(body.old_sheet_id)
    new_spec = lookup.get(body.new_sheet_id)
    if old_spec is None or new_spec is None:
        raise ValidationError("One of the two sheets is no longer in this comparison.")

    def find_record(path: str, page: int) -> object | None:
        for state in (session.old, session.new):
            for sheet in state.sheets:
                if sheet.abs_path == path and sheet.page_index == page:
                    return sheet
        return None

    dpi = 200
    options = RenderOptions(dpi=dpi, colour=False)
    old_render = render_page(old_spec[0], old_spec[1], options)
    new_render = render_page(new_spec[0], new_spec[1], options)

    record = find_record(new_spec[0], new_spec[1])
    denominator = None
    from engine.align.orchestrator import _scale_denominator

    if record is not None and getattr(record, "scale", None):
        denominator = _scale_denominator(record.scale)  # type: ignore[union-attr]

    result = apply_manual(
        [(point.old_x, point.old_y) for point in body.points],
        [(point.new_x, point.new_y) for point in body.points],
        old_img=old_render.grayscale if old_render.grayscale is not None else old_render.colour,
        new_img=new_render.grayscale if new_render.grayscale is not None else new_render.colour,
        scale_denominator=denominator,
        px_per_mm=ppm(dpi),
    )
    assert result.assessment is not None  # apply_manual always assesses

    # Replace the stored batch row at `index` so the review screen reflects it.
    if body.index is not None and 0 <= body.index < len(session.align_results):
        session.align_results[body.index] = result
    elif body.index is not None:
        raise ValidationError("That alignment row no longer exists. Re-run the batch first.")

    refs = list(session._align_pair_refs)
    return _row_for(body.index if body.index is not None else 0, result, refs)
