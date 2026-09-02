"""Routes for choosing folders and scanning them.

Thin by design: validate the request, call the session, return. All the logic
lives in `engine/core/session.py` and the modules under `engine/ingest/`.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine.core.enums import Side
from engine.core.session import ComparisonSession, get_session, suggest_output
from engine.core.workspace import validate_output_folder
from engine.utils.errors import ValidationError

router = APIRouter(prefix="/api", tags=["ingest"])


# ── Request bodies ─────────────────────────────────────────────────────


class SetFolderRequest(BaseModel):
    side: Side
    folder: str = Field(min_length=1)


class ValidateOutputRequest(BaseModel):
    folder: str = Field(min_length=1)


class SetOutputRequest(BaseModel):
    folder: str = Field(min_length=1)


class SetOptionsRequest(BaseModel):
    profile_id: str | None = None
    tolerance_mm: float | None = None


# ── Responses ──────────────────────────────────────────────────────────


class ScannedFileOut(BaseModel):
    filename: str
    rel_path: str
    abs_path: str
    size: int


class SideOut(BaseModel):
    side: Side
    folder: str | None
    headline: str
    file_count: int
    sheet_count: int
    identified_count: int
    attention_count: int
    is_scanning: bool
    error: str | None = None
    other_files: dict[str, int] = Field(default_factory=dict)
    skipped_count: int = 0


class SheetOut(BaseModel):
    """One row of a folder panel's drawing list."""

    abs_path: str
    filename: str
    page_index: int
    page_count: int
    drawing_no: str | None
    title: str | None
    revision: str | None
    scale: str | None
    sheet_size: str | None
    source_of_number: str
    source_explanation: str
    number_mismatch: bool
    is_readable: bool
    looks_scanned: bool
    warnings: list[str] = Field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────


def _side_out(session: ComparisonSession, side: Side) -> SideOut:
    state = session.side_state(side)
    return SideOut(
        side=side,
        folder=state.folder,
        headline=state.headline(),
        file_count=state.file_count,
        sheet_count=state.sheet_count,
        identified_count=state.identified_count,
        attention_count=state.attention_count,
        is_scanning=state.is_scanning,
        error=state.error,
        other_files=state.scan.other_by_extension() if state.scan else {},
        skipped_count=len(state.scan.skipped) if state.scan else 0,
    )


def _sheet_out(sheet: object) -> SheetOut:
    from engine.core.models import SheetRecord
    from engine.titleblock.field_extractor import SOURCE_EXPLANATION, NumberSource

    assert isinstance(sheet, SheetRecord)
    try:
        explanation = SOURCE_EXPLANATION[NumberSource(sheet.source_of_number)]
    except ValueError:
        explanation = ""

    return SheetOut(
        abs_path=sheet.abs_path,
        filename=sheet.filename,
        page_index=sheet.page_index,
        page_count=sheet.page_count,
        drawing_no=sheet.drawing_no,
        title=sheet.title,
        revision=sheet.revision,
        scale=sheet.scale,
        sheet_size=sheet.sheet_size,
        source_of_number=sheet.source_of_number,
        source_explanation=explanation,
        number_mismatch=sheet.number_mismatch,
        is_readable=sheet.is_readable,
        looks_scanned=sheet.looks_scanned,
        warnings=list(sheet.warnings),
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.post("/folder", response_model=SideOut, summary="Choose an issue folder")
def set_folder(body: SetFolderRequest) -> SideOut:
    """Record the folder and run the fast pass. Opens no PDFs."""
    session = get_session()

    other = Side.NEW if body.side is Side.OLD else Side.OLD
    other_folder = session.side_state(other).folder
    if other_folder and _same(other_folder, body.folder):
        raise ValidationError(
            "Both issues point at the same folder. Choose a different folder for one of them.",
            detail={"folder": body.folder},
        )

    session.set_folder(body.side, body.folder)
    return _side_out(session, body.side)


def _same(first: str, second: str) -> bool:
    import os

    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


# Registered before `/scan/{side}`: FastAPI matches in declaration order, so a
# parameterised route declared first would swallow this literal one and the
# cancel button would silently do nothing.
@router.post("/scan/cancel", summary="Stop the running scan")
def cancel_scan() -> dict[str, bool]:
    get_session().cancel_scanning()
    return {"cancelled": True}


@router.post("/scan/{side}", summary="Start the deep pass for one side")
def start_scan(side: Side) -> dict[str, str]:
    """Open every PDF in the background, streaming progress over the socket."""
    session = get_session()
    if session.side_state(side).folder is None:
        raise ValidationError("Choose a folder for this issue first.")

    return {"run_id": session.start_deep_pass(side)}


@router.get("/sides", response_model=list[SideOut], summary="State of both issue folders")
def get_sides() -> list[SideOut]:
    session = get_session()
    return [_side_out(session, Side.OLD), _side_out(session, Side.NEW)]


@router.get("/sheets/{side}", response_model=list[SheetOut], summary="Drawings found on one side")
def get_sheets(side: Side) -> list[SheetOut]:
    session = get_session()
    state = session.side_state(side)

    if state.sheets:
        return [_sheet_out(sheet) for sheet in state.sheets]

    # The deep pass has not finished yet, so show what the fast pass found.
    # The rows fill in with detail as results arrive.
    if state.scan is None:
        return []

    from engine.core.models import SheetRecord

    return [
        _sheet_out(
            SheetRecord(
                side=side,
                abs_path=item.abs_path,
                rel_path=item.rel_path,
                filename=item.filename,
                size=item.size,
            )
        )
        for item in state.scan.drawings
    ]


@router.get("/quarantine", summary="Files that could not be read")
def get_quarantine() -> list[dict[str, str]]:
    return get_session().quarantine_entries()


# ── Output folder ──────────────────────────────────────────────────────


@router.get("/output/suggestion", summary="A proposed output folder")
def output_suggestion() -> dict[str, str | None]:
    suggestion = suggest_output(get_session())
    return {"folder": str(suggestion) if suggestion else None}


@router.post("/output/validate", summary="Check an output folder before using it")
def validate_output(body: ValidateOutputRequest) -> dict[str, object]:
    session = get_session()
    return validate_output_folder(body.folder, session.old.folder, session.new.folder).as_dict()


@router.post("/output", summary="Confirm the output folder and build the workspace")
def set_output(body: SetOutputRequest) -> dict[str, object]:
    session = get_session()
    result = validate_output_folder(body.folder, session.old.folder, session.new.folder)

    if not result.is_valid:
        raise ValidationError(result.errors[0], detail=result.as_dict())

    workspace = session.set_output_folder(body.folder)
    return {"workspace": workspace.as_dict(), "validation": result.as_dict()}


# ── Options ────────────────────────────────────────────────────────────


@router.get("/profiles", summary="Sheet profiles that can be chosen")
def get_profiles() -> list[dict[str, str]]:
    from engine.titleblock.patterns import available_profiles

    return available_profiles()


@router.post("/options", summary="Set the sheet profile and tolerance")
def set_options(body: SetOptionsRequest) -> dict[str, object]:
    session = get_session()
    if body.profile_id is not None:
        session.profile_id = body.profile_id
    if body.tolerance_mm is not None:
        if body.tolerance_mm <= 0:
            raise ValidationError("Tolerance must be greater than zero millimetres.")
        session.tolerance_mm = body.tolerance_mm

    return {"profile_id": session.profile_id, "tolerance_mm": session.tolerance_mm}


@router.post("/session/reset", summary="Start a new comparison")
def reset() -> dict[str, bool]:
    from engine.core.session import reset_session

    reset_session()
    return {"reset": True}
