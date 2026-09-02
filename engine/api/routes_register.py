"""Routes for the drawing list, the register, and the Excel export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.enums import IssueType
from engine.core.models import ReconcileResult
from engine.core.session import get_session
from engine.core.workspace import write_audit_log
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["register"])


class DrawingListRequest(BaseModel):
    path: str = Field(min_length=1)


class MappingRequest(BaseModel):
    mapping: dict[str, str]
    sheet_name: str | None = None


class BuildRegisterRequest(BaseModel):
    issue_type: IssueType = IssueType.UNKNOWN


class CorrectNumberRequest(BaseModel):
    abs_path: str
    page_index: int = 0
    drawing_no: str = Field(min_length=1)
    #: Apply the same pattern to every other unidentified sheet.
    apply_to_all: bool = False


# ── The drawing list ───────────────────────────────────────────────────


@router.post("/drawing-list/preview", summary="Read a drawing list and propose a mapping")
def preview_drawing_list(body: DrawingListRequest) -> dict[str, Any]:
    """Parse the list and return a preview for the user to confirm.

    Nothing is used until the mapping is accepted: a mis-parsed list produces
    a register full of drawings that do not exist.
    """
    from engine.register.list_importer import import_drawing_list

    session = get_session()
    result = import_drawing_list(body.path)

    session.list_parse = result
    session.drawing_list_path = body.path
    return result.as_dict()


@router.post("/drawing-list/mapping", summary="Accept or correct the column mapping")
def apply_drawing_list_mapping(body: MappingRequest) -> dict[str, Any]:
    from engine.register.list_importer import parse_named_sheet, read_workbook_grids
    from engine.register.list_parser import apply_mapping, normalise_entries

    session = get_session()
    if session.list_parse is None or not session.drawing_list_path:
        raise ValidationError("Import a drawing list before setting its columns.")

    source = Path(session.drawing_list_path)
    parsed = session.list_parse

    if body.sheet_name and body.sheet_name != parsed.sheet_name:
        parsed = parse_named_sheet(source, body.sheet_name)

    if source.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        grid = read_workbook_grids(source)[parsed.sheet_name or ""]
        parsed = apply_mapping(parsed, grid, body.mapping)

    session.list_parse = parsed
    session.drawing_list = normalise_entries(parsed)
    session.register = None  # the register has to be rebuilt

    return parsed.as_dict()


@router.delete("/drawing-list", summary="Remove the imported drawing list")
def clear_drawing_list() -> dict[str, bool]:
    session = get_session()
    session.drawing_list = []
    session.drawing_list_path = None
    session.list_parse = None
    session.register = None
    return {"cleared": True}


# ── The register ───────────────────────────────────────────────────────


@router.post("/register", response_model=ReconcileResult, summary="Build the drawing register")
def build_register(body: BuildRegisterRequest) -> ReconcileResult:
    """Reconcile the two sets.

    May return `needs_issue_type_confirmation` instead of a register, when the
    two sets are very different sizes and nobody has said which kind of issue
    this is. Guessing there would make every old-only row say the wrong thing.
    """
    session = get_session()

    if not session.old.sheets and not session.new.sheets:
        raise ValidationError(
            "There are no drawings to reconcile yet. Choose both folders and let "
            "the scan finish first."
        )

    return session.build_register(body.issue_type)


@router.get("/register", response_model=ReconcileResult, summary="The register as it stands")
def get_register() -> ReconcileResult:
    session = get_session()
    if session.register is None:
        raise ValidationError("Build the register first.")
    return session.register


@router.post("/register/correct", summary="Correct a drawing number by hand")
def correct_number(body: CorrectNumberRequest) -> dict[str, Any]:
    """Set a drawing number the extractor could not read.

    One correction can fix a whole set: `apply_to_all` re-reads every other
    unidentified sheet using the pattern implied by this one.
    """
    from engine.titleblock.field_extractor import NumberSource, normalise_number

    session = get_session()
    corrected = 0

    for state in (session.old, session.new):
        for sheet in state.sheets:
            if sheet.abs_path == body.abs_path and sheet.page_index == body.page_index:
                sheet.drawing_no = body.drawing_no.strip().upper()
                sheet.normalised_no = normalise_number(sheet.drawing_no)
                sheet.source_of_number = str(NumberSource.USER)
                sheet.number_confidence = 1.0
                sheet.number_mismatch = False
                sheet.warnings = []
                corrected += 1

    if not corrected:
        raise ValidationError(
            "That sheet is no longer in the scan. Scan the folder again.",
            detail={"abs_path": body.abs_path},
        )

    applied = 0
    if body.apply_to_all:
        applied = _apply_pattern_to_unidentified(session, body.drawing_no)

    session.register = None
    return {"corrected": corrected, "also_applied": applied}


def _apply_pattern_to_unidentified(session: Any, example: str) -> int:
    """Re-read unidentified sheets using the shape of a number the user typed.

    The user has told us what this client's numbers look like. Turning that
    example into a pattern is what makes one correction fix a whole set.
    """
    import re

    from engine.titleblock.field_extractor import NumberSource, normalise_number

    # Turn "A-101" into a pattern that matches "A-102", "AB-1234" and so on.
    shape = re.escape(example.strip().upper())
    shape = re.sub(r"\\?\d+", r"\\d+", shape)
    shape = re.sub(r"[A-Z]+", r"[A-Z]+", shape)
    pattern = re.compile(rf"\b{shape}\b", re.IGNORECASE)

    applied = 0
    for state in (session.old, session.new):
        for sheet in state.sheets:
            if sheet.identified or not sheet.is_readable:
                continue

            from engine.extract.text_extractor import extract_document_text

            pages = extract_document_text(sheet.abs_path, pages=[sheet.page_index])
            page = pages.get(sheet.page_index)
            if page is None:
                continue

            match = pattern.search(page.joined())
            if match:
                sheet.drawing_no = match.group(0).upper()
                sheet.normalised_no = normalise_number(sheet.drawing_no)
                sheet.source_of_number = str(NumberSource.USER)
                sheet.number_confidence = 0.8
                sheet.warnings = []
                applied += 1

    return applied


# ── Export ─────────────────────────────────────────────────────────────


@router.post("/register/export", summary="Write the Excel register into the workspace")
def export_register() -> dict[str, str]:
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

    write_audit_log(session.workspace, session.audit_payload())
    return {"path": str(path), "folder": str(path.parent)}
