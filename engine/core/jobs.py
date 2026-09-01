"""The job engine: run the deep pass across CPU cores, with progress and cancel.

Three requirements shape this module.

**Use the cores.** Inspecting a PDF is CPU-bound, so it runs in a
`ProcessPoolExecutor`. Worker count defaults to `cpu_count() - 2` with a floor
of two, leaving headroom so the machine stays usable during a long run.

**Cancel must actually stop.** A cancel button that does nothing is worse than
no cancel button. Queued work is cancelled outright; work already inside a
worker is allowed to finish its current file, which takes well under a second.

**Survive a crash.** Every unit of work is a row in the `job` table, so a run
interrupted by a crash or a closed lid can be resumed rather than restarted.

Worker functions are module-level so they can be pickled: on Windows the pool
uses spawn, and each worker re-imports this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    BrokenExecutor,
    Future,
    ProcessPoolExecutor,
    wait,
)
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import Engine, select, update

from engine.core.enums import JobState
from engine.core.events import ProgressBus, RunReporter, Stage, progress_bus
from engine.storage.db import session_scope
from engine.storage.schema import Job

#: Leave two cores for the operating system and the UI.
RESERVED_CORES = 2
MIN_WORKERS = 2

#: Below this many files, the pool costs more than it saves.
POOL_THRESHOLD = 8


def default_worker_count() -> int:
    """How many worker processes to use on this machine."""
    return max(MIN_WORKERS, (os.cpu_count() or 4) - RESERVED_CORES)


# ── Cancellation ───────────────────────────────────────────────────────


class CancelToken:
    """A cancel flag that can be set from any thread."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()


# ── Worker functions (module level so they pickle) ─────────────────────


def inspect_file_task(path: str, size: int) -> dict[str, Any]:
    """Deep-inspect one PDF. Runs inside a worker process.

    Returns a plain dict rather than an object, because everything crossing a
    process boundary has to be picklable and stable.
    """
    from engine.ingest.pdf_inspector import inspect_pdf

    try:
        return inspect_pdf(path, size=size).as_dict()
    except Exception as exc:
        return {
            "path": path,
            "size": size,
            "page_count": 0,
            "pages": [],
            "is_encrypted": False,
            "needs_password": False,
            "layer_names": [],
            "producer": None,
            "is_readable": False,
            "error_note": f"This file could not be read. ({exc})",
        }


# ── Batch execution ────────────────────────────────────────────────────


@dataclass(slots=True)
class BatchResult:
    """Outcome of running one batch of work items."""

    results: list[dict[str, Any]] = field(default_factory=list)
    completed: int = 0
    total: int = 0
    cancelled: bool = False
    duration_seconds: float = 0.0
    from_cache: int = 0

    @property
    def was_complete(self) -> bool:
        return not self.cancelled and self.completed == self.total


def run_batch(
    items: Sequence[tuple[str, int]],
    *,
    run_id: str,
    stage: Stage = Stage.INSPECT,
    task: Callable[..., dict[str, Any]] = inspect_file_task,
    max_workers: int | None = None,
    cancel: CancelToken | None = None,
    bus: ProgressBus | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> BatchResult:
    """Run *task* over *items* across a process pool, reporting progress.

    Each item is `(path, size)`. Results stream to *on_result* as they arrive,
    so the UI fills in row by row rather than waiting for the whole batch.
    """
    cancel = cancel or CancelToken()
    reporter = RunReporter(run_id, stage, total=len(items), bus=bus or progress_bus)
    outcome = BatchResult(total=len(items))
    started = time.perf_counter()
    reporter.started()

    if not items:
        reporter.finished("Nothing to inspect.")
        return outcome

    def run_here(remaining: Sequence[tuple[str, int]]) -> None:
        """Do the work in this process. The fallback when there is no pool."""
        for path, size in remaining:
            if cancel.cancelled:
                outcome.cancelled = True
                return
            result = task(path, size)
            outcome.results.append(result)
            outcome.completed += 1
            if on_result:
                on_result(result)
            reporter.advance(os.path.basename(path))

    # A handful of files is faster in-process than paying for spawn.
    if len(items) < POOL_THRESHOLD:
        run_here(items)
    else:
        workers = max_workers or default_worker_count()
        logger.info("Deep pass | {} files across {} workers", len(items), workers)

        try:
            executor = ProcessPoolExecutor(max_workers=workers)
            futures: dict[Future[dict[str, Any]], str] = {
                executor.submit(task, path, size): path for path, size in items
            }
        except (BrokenExecutor, OSError, RuntimeError) as exc:
            # No usable process pool on this machine or in this context.
            # Doing the work slowly is far better than failing the run.
            logger.warning("Process pool unavailable ({}); running in this process", exc)
            run_here(items)
            outcome.duration_seconds = time.perf_counter() - started
            if outcome.cancelled:
                reporter.cancelled(f"Stopped after {outcome.completed} of {outcome.total} files.")
            else:
                reporter.finished(f"Read {outcome.completed} files.")
            return outcome

        try:
            pending: set[Future[dict[str, Any]]] = set(futures)

            while pending:
                if cancel.cancelled:
                    outcome.cancelled = True
                    for future in pending:
                        future.cancel()
                    break

                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    path = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.warning("Worker failed on {}: {}", path, exc)
                        result = {
                            "path": path,
                            "is_readable": False,
                            "error_note": f"This file could not be read. ({exc})",
                            "pages": [],
                            "page_count": 0,
                            "size": 0,
                            "is_encrypted": False,
                            "needs_password": False,
                            "layer_names": [],
                            "producer": None,
                        }
                    outcome.results.append(result)
                    outcome.completed += 1
                    if on_result:
                        on_result(result)
                    reporter.advance(os.path.basename(path))
        finally:
            executor.shutdown(wait=not outcome.cancelled, cancel_futures=True)

    outcome.duration_seconds = time.perf_counter() - started

    if outcome.cancelled:
        reporter.cancelled(f"Stopped after {outcome.completed} of {outcome.total} files.")
    else:
        reporter.finished(f"Read {outcome.completed} files in {outcome.duration_seconds:.1f}s.")
    return outcome


# ── Durable job records ────────────────────────────────────────────────


class JobStore:
    """The `job` table, so a long run survives a restart."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue_many(self, project_id: int, kind: str, payloads: Iterable[dict[str, Any]]) -> int:
        """Record every unit of work as queued. Returns how many were added."""
        rows = [
            Job(project_id=project_id, kind=kind, payload_json=json.dumps(payload))
            for payload in payloads
        ]
        if not rows:
            return 0
        with session_scope(self._engine) as session:
            session.add_all(rows)
        logger.debug("Queued {} '{}' jobs", len(rows), kind)
        return len(rows)

    def pending(self, project_id: int, kind: str | None = None) -> list[Job]:
        """Work that has not finished: queued, plus anything left running."""
        statement = select(Job).where(
            Job.project_id == project_id,
            Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        if kind is not None:
            statement = statement.where(Job.kind == kind)
        with session_scope(self._engine) as session:
            return list(session.scalars(statement))

    def mark(self, job_id: int, state: JobState, error: str | None = None) -> None:
        with session_scope(self._engine) as session:
            session.execute(
                update(Job).where(Job.id == job_id).values(state=str(state), error=error)
            )

    def reset_stale_running(self, project_id: int) -> int:
        """After a crash, anything left 'running' is really 'queued' again."""
        with session_scope(self._engine) as session:
            result = session.execute(
                update(Job)
                .where(Job.project_id == project_id, Job.state == JobState.RUNNING)
                .values(state=str(JobState.QUEUED))
            )
            count = int(result.rowcount or 0)
        if count:
            logger.info("Requeued {} jobs left running by a previous session", count)
        return count

    def counts(self, project_id: int) -> dict[str, int]:
        with session_scope(self._engine) as session:
            rows = session.execute(
                select(Job.state, Job.id).where(Job.project_id == project_id)
            ).all()
        counts: dict[str, int] = {}
        for state, _ in rows:
            counts[state] = counts.get(state, 0) + 1
        return counts

    def clear(self, project_id: int, kind: str | None = None) -> None:
        from sqlalchemy import delete

        statement = delete(Job).where(Job.project_id == project_id)
        if kind is not None:
            statement = statement.where(Job.kind == kind)
        with session_scope(self._engine) as session:
            session.execute(statement)
