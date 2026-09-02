"""Routes for producing output: the Excel register, and the audit log.

Everything the application writes goes into the output workspace the user
chose, never into either input folder, and an existing file is never
overwritten silently.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from engine.core.session import get_session
from engine.core.workspace import write_audit_log
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api/report", tags=["report"])


class ExportResponse(BaseModel):
    """Where the register was written."""

    path: str
    folder: str
    rows: int
    #: Said out loud so the user knows a second export did not replace the first.
    filename: str


@router.post("/register", response_model=ExportResponse, summary="Export the Excel register")
def export_register() -> ExportResponse:
    """Write the register workbook into `01_Register/` and log the run."""
    from engine import __version__
    from engine.report.register_xlsx import write_register_to_workspace

    session = get_session()

    if session.register is None:
        raise ValidationError("Build the register before exporting it.")
    if session.workspace is None:
        raise ValidationError(
            "Choose an output folder before exporting. Everything the "
            "application produces is written there."
        )

    path = write_register_to_workspace(
        session.workspace,
        session.register.rows,
        session.register.summary,
        quarantine=session.quarantine_entries(),
        context={
            "old_folder": session.old.folder or "",
            "new_folder": session.new.folder or "",
            "drawing_list": session.drawing_list_path or "",
            "profile": session.profile_id,
            "version": __version__,
        },
    )

    # The audit log is what makes the output defensible if it ever supports a
    # variation claim, so it is written with every export, not just the first.
    write_audit_log(session.workspace, session.audit_payload())

    return ExportResponse(
        path=str(path),
        folder=str(path.parent),
        rows=len(session.register.rows),
        filename=path.name,
    )


@router.get("/workspace", summary="Where output is being written")
def workspace_paths() -> dict[str, str] | dict[str, None]:
    session = get_session()
    if session.workspace is None:
        return {"root": None}
    return session.workspace.as_dict()
