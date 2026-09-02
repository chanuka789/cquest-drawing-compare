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
