"""Pydantic models for anything that crosses the API boundary.

Keep these in step with `ui/src/api/types.ts`. If a field changes here, it
changes there in the same commit.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.core.enums import AiMode, IssueType, RegisterStatus, Side


class AppPathsInfo(BaseModel):
    """Where the application keeps its own data on this machine."""

    root: str
    db: str
    logs: str
    cache: str
    profiles: str


class HealthResponse(BaseModel):
    """Returned by `GET /api/health`. The UI calls this before anything else."""

    status: str = Field(default="ok", description="Always 'ok' when the engine answers.")
    app_name: str
    version: str
    dev_mode: bool
    ai_mode: AiMode = Field(
        default=AiMode.OFFLINE,
        description="Offline is the default. AI never turns itself on.",
    )
    schema_version: int
    paths: AppPathsInfo


class ErrorBody(BaseModel):
    """The inner object of a structured error response."""

    code: str
    message: str
    detail: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Every failed request returns this shape."""

    error: ErrorBody


# ── Sheets and the register ────────────────────────────────────────────


class SheetRecord(BaseModel):
    """One drawing sheet, after intake. A page of a PDF, not a file."""

    side: Side = Side.NEW
    abs_path: str
    rel_path: str = ""
    filename: str = ""
    page_index: int = 0
    page_count: int = 1

    drawing_no: str | None = None
    #: Comparison form of the number: upper case, separators removed.
    normalised_no: str = ""
    source_of_number: str = "none"
    number_confidence: float = 0.0
    number_mismatch: bool = False
    filename_number: str | None = None

    title: str | None = None
    revision: str | None = None
    scale: str | None = None
    sheet_size: str | None = None

    content_hash: str | None = None
    size: int = 0
    is_readable: bool = True
    looks_scanned: bool = False
    error_note: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def identified(self) -> bool:
        return bool(self.drawing_no)

    @property
    def label(self) -> str:
        """What to show when there is no drawing number: the file name."""
        return self.drawing_no or self.filename


class ListEntry(BaseModel):
    """One row of an imported drawing list."""

    drawing_no: str
    normalised_no: str = ""
    title: str | None = None
    revision: str | None = None
    row_number: int | None = None


class RegisterRow(BaseModel):
    """One line of the drawing register: a drawing, across both issues."""

    drawing_no: str
    status: RegisterStatus
    title: str | None = None

    old_revision: str | None = None
    new_revision: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    old_page: int | None = None
    new_page: int | None = None

    source_of_number: str = "none"
    number_mismatch: bool = False
    #: True when this row must be looked at before the register is trusted.
    needs_attention: bool = False
    #: Plain-English explanation, written for the person reading the export.
    note: str = ""
    in_drawing_list: bool | None = None
    superseded_paths: list[str] = Field(default_factory=list)
    duplicate_paths: list[str] = Field(default_factory=list)

    @property
    def is_comparable(self) -> bool:
        """Whether Phase 4 should bother aligning and comparing this pair."""
        return self.status in {
            RegisterStatus.REVISED,
            RegisterStatus.SAME_REV_DIFFERENT_FILE,
            RegisterStatus.STATUS_CHANGE,
        }


class RegisterSummary(BaseModel):
    """Set-level counts and a sentence a person can read."""

    counts: dict[str, int] = Field(default_factory=dict)
    old_sheet_count: int = 0
    new_sheet_count: int = 0
    issue_type: IssueType = IssueType.UNKNOWN
    attention_count: int = 0
    comparable_count: int = 0
    sentence: str = ""


class IssueTypeQuestion(BaseModel):
    """Asked instead of guessing when the two sets are very different sizes."""

    old_count: int
    new_count: int
    question: str
    options: list[dict[str, str]] = Field(default_factory=list)


class ReconcileResult(BaseModel):
    """The register, or a question that has to be answered before there is one."""

    rows: list[RegisterRow] = Field(default_factory=list)
    summary: RegisterSummary = Field(default_factory=RegisterSummary)
    needs_issue_type_confirmation: bool = False
    issue_type_question: IssueTypeQuestion | None = None
