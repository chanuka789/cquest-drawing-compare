"""Phase 4 routes: batch alignment of the accepted match pairs.

The heavy work runs on the session's worker thread with progress exposed as
polled state (matching the rename pattern); results are snapshotted into the
workspace audit folder when the run completes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine.core.session import get_session
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["alignment"])


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


@router.get("/align/results", summary="The alignment results once the run is done")
def alignment_results() -> dict[str, Any]:
    session = get_session()
    if session.align_run["state"] != "done":
        raise ValidationError("Alignment has not finished yet. Check its status first.")
    rows: list[dict[str, Any]] = []
    for item in session.align_results:
        assessment = getattr(item, "assessment", None)
        rows.append(
            {
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
            }
        )
    return {"results": rows, "summary": dict(session.align_run["summary"])}
