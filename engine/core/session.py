"""The current comparison, held in memory.

This is a single-user desktop application: one window, one comparison at a
time. Keeping that state in one place means the API routes stay thin and the
UI never has to send back everything it was given.

The scan runs on a worker thread so the API never blocks. The UI watches
progress over the WebSocket and asks for results when a stage finishes.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from engine.core.enums import IssueType, Side
from engine.core.jobs import CancelToken
from engine.core.models import ListEntry, ReconcileResult, SheetRecord
from engine.core.pipeline import DeepPassResult, intake_folder
from engine.core.workspace import Workspace, create_workspace
from engine.ingest.folder_scanner import ScanResult, scan_folder
from engine.register.list_matcher import ListMatchResult
from engine.register.list_parser import ParseResult
from engine.storage.cache_store import CacheStore
from engine.titleblock.patterns import load_profile


@dataclass(slots=True)
class SideState:
    """Everything known about one issue folder."""

    folder: str | None = None
    scan: ScanResult | None = None
    sheets: list[SheetRecord] = field(default_factory=list)
    deep: DeepPassResult | None = None
    is_scanning: bool = False
    error: str | None = None

    @property
    def file_count(self) -> int:
        return self.scan.drawing_count if self.scan else 0

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def identified_count(self) -> int:
        return sum(1 for sheet in self.sheets if sheet.identified)

    @property
    def attention_count(self) -> int:
        return sum(
            1
            for sheet in self.sheets
            if not sheet.identified or not sheet.is_readable or sheet.number_mismatch
        )

    def headline(self) -> str:
        """The line above the drawing list, e.g. "147 files · 143 drawings · 4 need attention"."""
        if self.scan is None:
            return "Not scanned yet"
        parts = [f"{self.file_count} files", f"{self.sheet_count} drawings"]
        if self.attention_count:
            parts.append(f"{self.attention_count} need attention")
        return " · ".join(parts)


class ComparisonSession:
    """The one comparison this window is working on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.old = SideState()
        self.new = SideState()
        self.output_folder: str | None = None
        self.workspace: Workspace | None = None
        self.profile_id: str = "default"
        self.tolerance_mm: float = 25.0
        self.issue_type: IssueType = IssueType.UNKNOWN
        self.drawing_list_path: str | None = None
        self.drawing_list: list[ListEntry] = []
        self.list_parse: ParseResult | None = None
        self.list_match: ListMatchResult | None = None
        #: The user's literal answer to the issue-type question, which can
        #: be 'compare_reissued' as well as 'full' or 'partial'.
        self.issue_type_answer: str | None = None
        self.register: ReconcileResult | None = None
        self._cache: CacheStore | None = None
        self._cancel = CancelToken()
        self._threads: dict[str, threading.Thread] = {}
        #: Phase 3 matching state. See :meth:`run_matching`.
        self.matching: dict[str, object] = {
            "state": "idle",
            "run_id": None,
            "current": 0,
            "total": 0,
            "message": None,
            "error": None,
        }
        self.match_result: object | None = None
        #: old-sheet key -> "accepted" | "rejected" (a pair is its old sheet).
        self.match_decisions: dict[str, str] = {}
        #: old-sheet key -> chosen new-sheet key (manual or alternative picks).
        self.manual_pairs: dict[str, str] = {}
        #: Phase 3 rename state. See :meth:`run_renames` / :meth:`run_undo`.
        self.rename_run: dict[str, object] = {
            "kind": None,
            "state": "idle",
            "run_id": None,
            "current": 0,
            "total": 0,
            "current_item": None,
            "completed": 0,
            "failed": 0,
            "message": None,
            "error": None,
            "failed_items": [],
            "undo_log_path": None,
        }
        self.rename_plan: object | None = None
        self._rename_cancel = CancelToken()

    # -- sides ---------------------------------------------------------

    def side_state(self, side: Side) -> SideState:
        return self.old if side is Side.OLD else self.new

    def set_folder(self, side: Side, folder: str) -> ScanResult:
        """Record a chosen folder and run the fast pass immediately.

        The fast pass opens no PDFs, so the file list is on screen in well
        under two seconds and the deep pass can run behind it.
        """
        state = self.side_state(side)
        state.folder = folder
        state.error = None
        state.sheets = []
        state.deep = None
        state.scan = scan_folder(folder)
        self.register = None  # any existing register is now out of date
        return state.scan

    # -- the deep pass -------------------------------------------------

    def start_deep_pass(self, side: Side) -> str:
        """Start the deep pass on a worker thread. Returns a run id."""
        state = self.side_state(side)
        if state.folder is None:
            raise ValueError("Choose a folder for this issue first.")

        run_id = f"{side}-{uuid.uuid4().hex[:8]}"
        self._cancel.reset()

        def work() -> None:
            state.is_scanning = True
            try:
                sheets, deep = intake_folder(
                    state.folder or "",
                    side=side,
                    run_id=run_id,
                    cache=self.cache,
                    cancel=self._cancel,
                    profile=load_profile(self.profile_id),
                )
                state.sheets = sheets
                state.deep = deep
            except Exception:
                logger.exception("Deep pass failed for {}", side)
                state.error = (
                    "The drawings in this folder could not be read. Check the "
                    "folder is still reachable, then try again. The details are "
                    "in the log file."
                )
            finally:
                state.is_scanning = False

        thread = threading.Thread(target=work, name=f"cqdc-scan-{side}", daemon=True)
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def cancel_scanning(self) -> None:
        """Stop any running deep pass. Must take effect within a second."""
        logger.info("Cancel requested")
        self._cancel.cancel()

    @property
    def is_scanning(self) -> bool:
        return self.old.is_scanning or self.new.is_scanning

    def wait_for_scans(self, timeout: float = 120.0) -> None:
        """Block until the running scans finish. Used by tests, not the UI."""
        for thread in list(self._threads.values()):
            thread.join(timeout=timeout)

    # -- output workspace ----------------------------------------------

    def set_output_folder(self, folder: str) -> Workspace:
        """Create the workspace, so the user can see what will happen."""
        self.output_folder = folder
        self.workspace = create_workspace(folder)
        self._cache = None  # the cache lives in the workspace
        return self.workspace

    @property
    def cache(self) -> CacheStore | None:
        """The scan cache, kept in the workspace so it travels with the job."""
        if self.workspace is None:
            return None
        if self._cache is None:
            self._cache = CacheStore(self.workspace.scan_cache_path)
        return self._cache

    # -- the register --------------------------------------------------

    def build_register(self, issue_type: IssueType | None = None) -> ReconcileResult:
        from engine.register.list_matcher import apply_drawing_list
        from engine.register.reconciler import reconcile

        if issue_type is not None:
            self.issue_type = issue_type

        # Priority 4 of the drawing-number order: the list fills gaps the
        # sheets themselves could not, and never overwrites a title block.
        if self.drawing_list:
            self.list_match = apply_drawing_list(
                [*self.old.sheets, *self.new.sheets], self.drawing_list
            )

        self.register = reconcile(
            self.old.sheets,
            self.new.sheets,
            drawing_list=self.drawing_list or None,
            issue_type=self.issue_type,
        )
        return self.register

    # -- Phase 3: sheet matching ---------------------------------------

    def run_matching(self) -> str:
        """Match the two sides on a worker thread. Returns a run id."""
        if self.matching["state"] == "running":
            raise ValueError("A matching run is already in progress.")
        if not self.old.sheets and not self.new.sheets:
            raise ValueError("There are no drawings to match yet.")

        run_id = f"match-{uuid.uuid4().hex[:8]}"
        self.matching = {
            "state": "running",
            "run_id": run_id,
            "current": 0,
            "total": len(self.old.sheets) + len(self.new.sheets),
            "message": "Starting…",
            "error": None,
        }
        self.match_result = None
        self.match_decisions = {}
        self.manual_pairs = {}

        def work() -> None:
            from engine.core.events import RunReporter, Stage
            from engine.naming import matcher as matcher_module
            from engine.naming.fingerprint_cache import fingerprints_memo, resolver_from_memo

            reporter = RunReporter(run_id, Stage.MATCH, total=int(self.matching["total"]))
            try:
                reporter.started()
                memo = fingerprints_memo(
                    [*self.old.sheets, *self.new.sheets], self.cache
                )
                result = matcher_module.match_sets(
                    self.old.sheets,
                    self.new.sheets,
                    fingerprint_for=resolver_from_memo(memo),
                )
                self.match_result = result
                summary = result.summary()
                self.matching = {
                    "state": "ready",
                    "run_id": run_id,
                    "current": int(self.matching["total"]),
                    "total": int(self.matching["total"]),
                    "message": (
                        f"{summary['auto']} auto-matched, {summary['review']} need "
                        f"review, {summary['old_unmatched']} unmatched."
                    ),
                    "error": None,
                }
                reporter.finished(str(self.matching["message"]))
            except Exception as exc:
                logger.exception("Matching failed")
                self.matching = {
                    "state": "failed",
                    "run_id": run_id,
                    "current": 0,
                    "total": int(self.matching["total"]),
                    "message": None,
                    "error": "Matching could not finish. Check the log file for details.",
                }
                reporter.failed(str(exc))

        thread = threading.Thread(target=work, name="cqdc-match", daemon=True)
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def wait_for_matching(self, timeout: float = 300.0) -> None:
        """Block until the matching thread finishes. Used by tests."""
        run_id = self.matching.get("run_id")
        thread = self._threads.get(str(run_id)) if run_id else None
        if thread is not None:
            thread.join(timeout=timeout)

    def matching_status(self) -> dict[str, object]:
        return dict(self.matching)

    def apply_match_decisions(
        self,
        accepted: list[str] | None = None,
        rejected: list[str] | None = None,
        manual: list[dict[str, str]] | None = None,
    ) -> dict[str, int]:
        """Record the user's review of the match result."""
        for key in accepted or []:
            self.match_decisions[str(key)] = "accepted"
            self.manual_pairs.pop(str(key), None)
        for key in rejected or []:
            self.match_decisions[str(key)] = "rejected"
            self.manual_pairs.pop(str(key), None)
        for item in manual or []:
            old_key = str(item.get("old_key") or "")
            new_key = str(item.get("new_key") or "")
            if old_key and new_key:
                self.match_decisions[old_key] = "accepted"
                self.manual_pairs[old_key] = new_key

        from engine.naming.matcher import MatchResult

        result = self.match_result
        unresolved = 0
        if isinstance(result, MatchResult):
            for pair in result.review_pairs:
                key = f"{pair.old.abs_path}#{pair.old.page_index}"
                if key not in self.match_decisions:
                    unresolved += 1
        return {
            "accepted": sum(1 for value in self.match_decisions.values() if value == "accepted"),
            "rejected": sum(1 for value in self.match_decisions.values() if value == "rejected"),
            "manual": len(self.manual_pairs),
            "unresolved_review": unresolved,
        }

    def finalize_matching(self) -> str | None:
        """Snapshot the match review into the workspace audit folder."""
        import json
        from datetime import UTC, datetime

        from engine.naming.matcher import MatchResult, result_as_dict

        if self.workspace is None:
            raise ValueError("Choose an output folder before saving the match review.")

        payload: dict[str, object] = {
            "written_at": datetime.now(UTC).isoformat(),
            "old_folder": self.old.folder,
            "new_folder": self.new.folder,
            "decisions": dict(self.match_decisions),
            "manual_pairs": dict(self.manual_pairs),
        }
        if isinstance(self.match_result, MatchResult):
            payload["match"] = result_as_dict(self.match_result)

        self.workspace.audit_dir.mkdir(parents=True, exist_ok=True)
        target = self.workspace.audit_dir / "matching.json"
        target.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        return str(target)

    # -- Phase 3: rename --------------------------------------------------

    def rename_context(self) -> dict[str, str]:
        """Token context from the active sheet profile (project/originator)."""
        try:
            from engine.titleblock.patterns import load_profile

            return dict(load_profile(self.profile_id).naming)
        except Exception:
            return {}

    def plan_renames(
        self,
        template: str,
        *,
        mode: str = "copy",
        use_discipline_folders: bool = False,
    ) -> object:
        """Build the rename plan against the current (received) issue set."""
        from engine.naming.rename_planner import build_plan

        output_dir = None
        if self.workspace is not None:
            output_dir = (
                self.workspace.renamed_dir
                if mode == "copy"
                else Path(self.new.folder or "").parent
            )
        plan = build_plan(
            self.new.sheets,
            template,
            self.rename_context(),
            output_dir if output_dir is not None else "",
            mode=mode,
            use_discipline_folders=use_discipline_folders,
        )
        self.rename_plan = plan
        return plan

    def run_renames(self, *, i_understand: bool = False) -> str:
        """Apply the last rename plan on a worker thread (with progress)."""
        if self.rename_run["state"] == "running":
            raise ValueError("A rename or undo is already running.")
        if self.rename_plan is None:
            raise ValueError("Build a rename plan before applying it.")
        if self.workspace is None:
            raise ValueError("Choose an output folder before renaming.")

        run_id = f"rename-{uuid.uuid4().hex[:8]}"
        self._rename_cancel.reset()
        self.rename_run = {
            "kind": "rename",
            "state": "running",
            "run_id": run_id,
            "current": 0,
            "total": 0,
            "current_item": None,
            "completed": 0,
            "failed": 0,
            "message": None,
            "error": None,
            "failed_items": [],
            "undo_log_path": None,
        }
        plan = self.rename_plan
        actions = list(getattr(plan, "actions", []) or [])
        total = len(actions)
        self.rename_run["total"] = total
        log_path = self.workspace.audit_dir / "rename_log.json"

        def work() -> None:
            from engine.naming.rename_executor import execute_plan

            try:
                outcome = execute_plan(
                    plan,
                    log_path,
                    mode=str(self.rename_plan_mode()),
                    i_understand=i_understand,
                    cancel=self._rename_cancel,
                    progress=self._rename_progress,
                    input_folders=[
                        folder for folder in (self.old.folder, self.new.folder) if folder
                    ],
                )
                state = "cancelled" if outcome.cancelled else "done"
                self.rename_run.update(
                    {
                        "state": state,
                        "current": outcome.completed,
                        "completed": outcome.completed,
                        "failed": len(outcome.failed),
                        "failed_items": outcome.failed,
                        "undo_log_path": str(outcome.undo_log_path)
                        if outcome.undo_log_path
                        else None,
                        "message": f"Renamed {outcome.completed} file(s)." if not outcome.cancelled
                        else f"Stopped after {outcome.completed} file(s).",
                    }
                )
            except Exception as exc:
                logger.exception("Rename failed")
                self.rename_run.update(
                    {"state": "failed", "error": str(exc), "message": None}
                )

        thread = threading.Thread(target=work, name="cqdc-rename", daemon=True)
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def rename_plan_mode(self) -> str:
        """The mode the stored plan was built with ('copy' | 'in_place')."""
        plan = self.rename_plan
        mode = getattr(plan, "mode", None)
        return str(mode) if mode is not None else "copy"

    def _rename_progress(self, completed: int, total: int, current_item: str) -> None:
        self.rename_run.update(
            {
                "current": completed,
                "total": total,
                "current_item": current_item,
                "completed": completed,
                "message": f"Renaming… {current_item}",
            }
        )

    def cancel_rename(self) -> bool:
        if self.rename_run["state"] == "running":
            self._rename_cancel.cancel()
            return True
        return False

    def run_undo(self) -> str:
        """Reverse the last rename on a worker thread."""
        if self.rename_run["state"] == "running":
            raise ValueError("A rename or undo is already running.")
        if self.workspace is None:
            raise ValueError("No workspace for this run.")

        run_id = f"undo-{uuid.uuid4().hex[:8]}"
        self._rename_cancel.reset()
        self.rename_run = {
            "kind": "undo",
            "state": "running",
            "run_id": run_id,
            "current": 0,
            "total": 0,
            "current_item": None,
            "completed": 0,
            "failed": 0,
            "message": None,
            "error": None,
            "failed_items": [],
            "undo_log_path": None,
        }
        log_path = self.workspace.audit_dir / "rename_log.json"

        def work() -> None:
            from engine.naming.undo_log import UndoLog

            try:
                undo_log = UndoLog(log_path)
                outcome = undo_log.undo(verify=True, cancel=self._rename_cancel)
                self.rename_run.update(
                    {
                        "state": "cancelled" if outcome.stopped else "done",
                        "current": outcome.reversed,
                        "completed": outcome.reversed,
                        "failed": len(outcome.failed),
                        "failed_items": outcome.failed,
                        "undo_log_path": str(log_path),
                        "message": outcome.reason
                        or f"Undid {outcome.reversed} rename(s).",
                    }
                )
            except Exception as exc:
                logger.exception("Undo failed")
                self.rename_run.update({"state": "failed", "error": str(exc)})

        thread = threading.Thread(target=work, name="cqdc-undo", daemon=True)
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def rename_status(self) -> dict[str, object]:
        return dict(self.rename_run)

    def quarantine_entries(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for state in (self.old, self.new):
            if state.deep is not None:
                entries.extend(state.deep.quarantine.as_list())
        return entries

    def audit_payload(self) -> dict[str, object]:
        """What went into this run, for the audit log."""
        return {
            "old_folder": self.old.folder,
            "new_folder": self.new.folder,
            "output_folder": self.output_folder,
            "drawing_list": self.drawing_list_path,
            "profile": self.profile_id,
            "tolerance_mm": self.tolerance_mm,
            "issue_type": str(self.issue_type),
            "issue_type_answer": self.issue_type_answer or str(self.issue_type),
            "drawing_list_matches": self.list_match.total if self.list_match else 0,
            "old_file_count": self.old.file_count,
            "new_file_count": self.new.file_count,
            "old_sheet_count": self.old.sheet_count,
            "new_sheet_count": self.new.sheet_count,
            "counts": self.register.summary.counts if self.register else {},
            "quarantined": len(self.quarantine_entries()),
        }

    def reset(self) -> None:
        with self._lock:
            self.old = SideState()
            self.new = SideState()
            self.register = None
            self.drawing_list = []
            self.drawing_list_path = None
            self.list_parse = None
            self.list_match = None


#: One session per running application.
_session = ComparisonSession()


def get_session() -> ComparisonSession:
    return _session


def reset_session() -> ComparisonSession:
    """Start a fresh comparison. Used by the UI and by the tests."""
    global _session
    if _session._cache is not None:
        _session._cache.close()
    _session = ComparisonSession()
    return _session


def suggest_output(session: ComparisonSession) -> Path | None:
    """Propose an output folder once both issue folders are known."""
    from engine.core.workspace import suggest_output_folder

    if not session.old.folder or not session.new.folder:
        return None

    def common_revision(state: SideState) -> str | None:
        revisions = [sheet.revision for sheet in state.sheets if sheet.revision]
        if not revisions:
            return None
        return max(set(revisions), key=revisions.count)

    return suggest_output_folder(
        session.old.folder,
        session.new.folder,
        common_revision(session.old),
        common_revision(session.new),
    )
