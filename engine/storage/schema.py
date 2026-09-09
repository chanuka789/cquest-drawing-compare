"""SQLite schema. One database file per project.

Two design rules from the project plan are enforced here:

* Every AI call is recorded in :class:`AiCall`, so a client can be shown
  exactly what left the machine.
* :attr:`Pair.status` is set before any comparison runs. Set reconciliation
  must still be correct when comparison fails.

Phase 1 uses :class:`Meta`, :class:`Project`, :class:`File` and :class:`Sheet`.
The remaining tables are created now so the file format does not change under
the user in Phase 2 and later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

#: Bumped whenever the schema changes. Stored in the `meta` table of every
#: project file so a future version can migrate an older project.
SCHEMA_VERSION = 2


def utc_now() -> datetime:
    """Timezone-aware creation timestamp."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime that is always UTC, in the database and in Python.

    SQLite has no timezone type and hands back a naive datetime. Storing local
    times in an audit trail is not defensible, so every timestamp is converted
    to UTC on the way in and marked as UTC on the way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for every table in a project database."""

    type_annotation_map: ClassVar[dict[type, object]] = {datetime: UtcDateTime}


class Meta(Base):
    """Key/value header of the project file. Holds the schema version."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Project(Base):
    """One comparison project: two issue folders and the settings used."""

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created: Mapped[datetime] = mapped_column(default=utc_now)
    old_folder: Mapped[str | None] = mapped_column(Text, default=None)
    new_folder: Mapped[str | None] = mapped_column(Text, default=None)
    profile_id: Mapped[str | None] = mapped_column(String(64), default=None)
    settings_json: Mapped[str | None] = mapped_column(Text, default=None)

    files: Mapped[list[File]] = relationship(back_populates="project", cascade="all, delete-orphan")


class File(Base):
    """A file found on disk. Never modified — the engine is read-only here."""

    __tablename__ = "file"
    __table_args__ = (
        UniqueConstraint("project_id", "side", "abs_path", name="uq_file_project_side_path"),
        Index("ix_file_hash", "hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    side: Mapped[str] = mapped_column(String(8))  # Side
    abs_path: Mapped[str] = mapped_column(Text)
    rel_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    hash: Mapped[str | None] = mapped_column(String(64), default=None)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    is_readable: Mapped[bool] = mapped_column(default=True)
    error_note: Mapped[str | None] = mapped_column(Text, default=None)

    project: Mapped[Project] = relationship(back_populates="files")
    sheets: Mapped[list[Sheet]] = relationship(back_populates="file", cascade="all, delete-orphan")


class Sheet(Base):
    """One drawing. A multi-page PDF produces one sheet per page."""

    __tablename__ = "sheet"
    __table_args__ = (
        UniqueConstraint("file_id", "page_index", name="uq_sheet_file_page"),
        Index("ix_sheet_dwg_number", "dwg_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("file.id", ondelete="CASCADE"))
    page_index: Mapped[int] = mapped_column(Integer, default=0)
    dwg_number: Mapped[str | None] = mapped_column(String(120), default=None)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    revision: Mapped[str | None] = mapped_column(String(32), default=None)
    scale: Mapped[str | None] = mapped_column(String(32), default=None)
    sheet_size: Mapped[str | None] = mapped_column(String(16), default=None)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    issue_date: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Where the drawing number came from: title_block, filename, drawing_list, user.
    source_of_number: Mapped[str | None] = mapped_column(String(32), default=None)

    file: Mapped[File] = relationship(back_populates="sheets")


class Pair(Base):
    """The reconciliation result for one drawing across the two issues."""

    __tablename__ = "pair"
    __table_args__ = (Index("ix_pair_project_status", "project_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    old_sheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("sheet.id", ondelete="SET NULL"), default=None
    )
    new_sheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("sheet.id", ondelete="SET NULL"), default=None
    )
    match_method: Mapped[str | None] = mapped_column(String(32), default=None)  # MatchMethod
    confidence: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="ambiguous")  # PairStatus


class Alignment(Base):
    """The transform that puts the old sheet on top of the new one."""

    __tablename__ = "alignment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pair.id", ondelete="CASCADE"))
    matrix_json: Mapped[str | None] = mapped_column(Text, default=None)
    method: Mapped[str | None] = mapped_column(String(32), default=None)  # AlignmentMethod
    rms_error: Mapped[float | None] = mapped_column(default=None)
    quality: Mapped[float | None] = mapped_column(default=None)
    #: False means the alignment was rejected. A bad alignment is refused,
    #: never reported as a comparison result.
    accepted: Mapped[bool] = mapped_column(default=False)


class Change(Base):
    """One detected change region. Coordinates are in sheet space."""

    __tablename__ = "change"
    __table_args__ = (Index("ix_change_pair_status", "pair_id", "user_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pair.id", ondelete="CASCADE"))
    x: Mapped[float] = mapped_column(default=0.0)
    y: Mapped[float] = mapped_column(default=0.0)
    w: Mapped[float] = mapped_column(default=0.0)
    h: Mapped[float] = mapped_column(default=0.0)
    type: Mapped[str | None] = mapped_column(String(16), default=None)  # ChangeType
    severity: Mapped[str | None] = mapped_column(String(16), default=None)  # Severity
    is_cosmetic: Mapped[bool] = mapped_column(default=False)
    #: True when the designer drew a revision cloud around it. Changed but not
    #: clouded is the finding that matters commercially.
    is_clouded: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    user_status: Mapped[str] = mapped_column(String(16), default="open")  # UserStatus

    texts: Mapped[list[ChangeText]] = relationship(
        back_populates="change", cascade="all, delete-orphan"
    )


class ChangeText(Base):
    """The old and new text behind a change: a dimension, a tag or a note."""

    __tablename__ = "change_text"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("change.id", ondelete="CASCADE"))
    old_text: Mapped[str | None] = mapped_column(Text, default=None)
    new_text: Mapped[str | None] = mapped_column(Text, default=None)
    kind: Mapped[str | None] = mapped_column(String(16), default=None)  # TextChangeKind

    change: Mapped[Change] = relationship(back_populates="texts")


class BoqLink(Base):
    """Link from a change to a bill of quantities item.

    `confirmed_by_user` stays False until a human agrees. A cost is never
    reported from an unconfirmed link.
    """

    __tablename__ = "boq_link"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("change.id", ondelete="CASCADE"))
    boq_item_code: Mapped[str | None] = mapped_column(String(64), default=None)
    qty: Mapped[float | None] = mapped_column(default=None)
    rate: Mapped[float | None] = mapped_column(default=None)
    amount: Mapped[float | None] = mapped_column(default=None)
    confidence: Mapped[float] = mapped_column(default=0.0)
    confirmed_by_user: Mapped[bool] = mapped_column(default=False)


class RenameLog(Base):
    """Reversible record of every rename. Originals are never touched.

    One row per operation, written *before* the operation runs. The source
    hash is what makes an undo safe: if the file changed after the rename,
    the undo refuses to overwrite newer work.
    """

    __tablename__ = "rename_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    #: Order of operations; undo runs in reverse sequence.
    seq: Mapped[int] = mapped_column(Integer, default=0)
    #: 'copy' writes a renamed copy into the output workspace; 'in_place'
    #: renames the file where it is. Copy is the default and the safe one.
    operation: Mapped[str] = mapped_column(String(16), default="copy")
    source_path: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)
    #: Hash of the source file before the operation, for verified undo.
    source_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    success: Mapped[bool] = mapped_column(default=False)
    applied_at: Mapped[datetime] = mapped_column(default=utc_now)
    reversed_at: Mapped[datetime | None] = mapped_column(default=None)


class AiCall(Base):
    """Audit record of every AI call, including cached hits."""

    __tablename__ = "ai_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    cached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class Job(Base):
    """Queued unit of work. Survives a restart so a long run can resume."""

    __tablename__ = "job"
    __table_args__ = (Index("ix_job_project_state", "project_id", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(48))
    payload_json: Mapped[str | None] = mapped_column(Text, default=None)
    state: Mapped[str] = mapped_column(String(16), default="queued")  # JobState
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)
