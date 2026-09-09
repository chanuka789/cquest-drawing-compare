"""Mirror the JSON rename log into a per-workspace SQLite file.

The plan requires every rename operation to be recorded twice: in
``_audit/rename_log.json`` (the human-readable, portable record) and in the
SQLite ``rename_log`` table (the durable, queryable one). This module opens —
or creates — a project database inside the workspace at
``_audit/project.cqdc``, attaches a :class:`Project` row for the comparison,
and hands back a small callable the undo log calls once per settled entry.

The database travels with the output folder, so a comparison handed to
another machine keeps its audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from sqlalchemy import Engine, select

from engine.core.workspace import Workspace
from engine.storage.db import create_project_db, open_project_db, session_scope
from engine.storage.schema import Project, RenameLog


def rename_db_path(workspace: Workspace) -> Path:
    """Where the mirror database for one workspace lives."""
    return workspace.audit_dir / "project.cqdc"


def _ensure_project(engine: Engine, workspace: Workspace) -> int:
    """Return (creating if needed) the project id for this comparison."""
    with session_scope(engine) as session:
        project = session.scalar(
            select(Project).where(Project.name == workspace.root.name).limit(1)
        )
        if project is None:
            project = Project(
                name=workspace.root.name,
                old_folder="",
                new_folder="",
            )
            session.add(project)
            session.flush()
        return int(project.id)


def make_rename_log_mirror(workspace: Workspace) -> Callable[[dict[str, object]], None] | None:
    """A ``db_mirror`` callable for :class:`UndoLog`, or None if unusable.

    Each call opens the workspace database once, records one row, and closes
    again — rename runs are human-paced and correctness beats microsecond
    batching here.
    """
    path = rename_db_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = open_project_db(path) if path.exists() else create_project_db(path)
        project_id = _ensure_project(engine, workspace)

        def mirror(entry: dict[str, object]) -> None:
            try:
                with session_scope(engine) as session:
                    existing = session.scalar(
                        select(RenameLog).where(
                            RenameLog.project_id == project_id,
                            RenameLog.seq == int(entry.get("seq", 0)),
                        )
                    )
                    if existing is not None:
                        return
                    session.add(
                        RenameLog(
                            project_id=project_id,
                            seq=int(entry.get("seq", 0)),
                            operation=str(entry.get("operation", "copy"))[:16],
                            source_path=str(entry.get("source_path", "")),
                            target_path=str(entry.get("target_path", "")),
                            source_hash=entry.get("source_hash"),
                            success=bool(entry.get("success", False)),
                        )
                    )
            except Exception as exc:  # a broken mirror must never fail a rename
                logger.warning("Could not mirror rename log entry: {}", exc)

        return mirror
    except Exception as exc:
        logger.warning("Rename log mirror unavailable ({}): {}", type(exc).__name__, exc)
        return None
