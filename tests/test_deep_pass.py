"""Deep inspection, the scan cache, the job queue and progress events."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from engine.core.enums import JobState
from engine.core.events import EventKind, ProgressBus, ProgressEvent, RunReporter, Stage
from engine.core.jobs import CancelToken, JobStore, default_worker_count, run_batch
from engine.core.pipeline import INSPECT_KIND, deep_inspect
from engine.ingest.folder_scanner import scan_folder
from engine.ingest.pdf_inspector import PdfInfo, detect_sheet_size, inspect_pdf
from engine.ingest.quarantine import Quarantine, QuarantineReason
from engine.storage.cache_store import CacheKey, CacheStore
from tests.fixture_builder import (
    SheetSpec,
    build_corrupt_pdf,
    build_pdf,
    build_scanned_pdf,
)


@pytest.fixture(scope="module")
def mixed_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder holding every awkward case at once."""
    root = tmp_path_factory.mktemp("mixed")
    build_pdf(root / "A-101.pdf", [SheetSpec(drawing_no="A-101")])
    build_pdf(
        root / "A-102-multipage.pdf",
        [
            SheetSpec(drawing_no="A-102"),
            SheetSpec(drawing_no="A-103"),
            SheetSpec(drawing_no="A-104"),
        ],
    )
    build_pdf(root / "locked.pdf", password="secret")
    build_corrupt_pdf(root / "damaged.pdf")
    build_scanned_pdf(root / "photocopy.pdf")
    (root / "empty.pdf").write_bytes(b"")
    return root


# ── Sheet size ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (841, 1189, "A0"),
        (594, 841, "A1"),
        (841, 594, "A1"),  # landscape is the same size
        (847, 594, "A1"),  # the real fixture, 6 mm oversize
        (420, 297, "A3"),
        (210, 297, "A4"),
        (1000, 1000, "Custom"),
    ],
)
def test_sheet_size_detection(width, height, expected):
    assert detect_sheet_size(width, height) == expected


# ── Inspection ─────────────────────────────────────────────────────────


def test_a_normal_drawing_is_read(mixed_folder: Path):
    info = inspect_pdf(mixed_folder / "A-101.pdf")

    assert info.is_readable
    assert info.page_count == 1
    assert info.has_text_layer
    assert not info.looks_scanned
    assert info.pages[0].sheet_size == "A1"


def test_a_multipage_file_reports_every_page(mixed_folder: Path):
    """One file can be twenty drawings. The unit of work is the sheet."""
    info = inspect_pdf(mixed_folder / "A-102-multipage.pdf")

    assert info.is_multipage
    assert info.page_count == 3
    assert len(info.pages) == 3


def test_a_password_protected_file_is_reported_not_raised(mixed_folder: Path):
    info = inspect_pdf(mixed_folder / "locked.pdf")

    assert info.needs_password
    assert info.is_encrypted
    assert not info.is_readable
    assert "password" in info.error_note.lower()
    assert "Ask the sender" in info.error_note  # says what to do


def test_a_damaged_file_is_reported_not_raised(mixed_folder: Path):
    info = inspect_pdf(mixed_folder / "damaged.pdf")

    assert not info.is_readable
    assert info.error_note


def test_a_scanned_sheet_is_detected(mixed_folder: Path):
    """Detect scanned sheets early and warn honestly, never fail silently."""
    info = inspect_pdf(mixed_folder / "photocopy.pdf")

    assert info.is_readable
    assert info.looks_scanned
    assert not info.has_text_layer


def test_a_missing_file_is_reported(tmp_path: Path):
    info = inspect_pdf(tmp_path / "gone.pdf")
    assert not info.is_readable
    assert info.error_note


def test_pdf_info_round_trips_through_a_dict(mixed_folder: Path):
    """The dict form crosses a process boundary and goes into the cache."""
    original = inspect_pdf(mixed_folder / "A-102-multipage.pdf")
    restored = PdfInfo.from_dict(original.as_dict())

    assert restored.page_count == original.page_count
    assert restored.pages[0].sheet_size == original.pages[0].sheet_size
    assert restored.has_text_layer == original.has_text_layer


# ── Quarantine ─────────────────────────────────────────────────────────


def test_quarantine_sorts_files_by_reason(mixed_folder: Path):
    quarantine = Quarantine()
    for name in ("A-101.pdf", "locked.pdf", "damaged.pdf", "empty.pdf"):
        quarantine.add_from_inspection(inspect_pdf(mixed_folder / name))

    reasons = quarantine.by_reason()
    assert reasons[QuarantineReason.PASSWORD_PROTECTED] == 1
    assert QuarantineReason.DAMAGED in reasons or QuarantineReason.EMPTY in reasons
    assert len(quarantine) == 3  # the good file is not quarantined


def test_quarantine_summary_is_a_sentence():
    quarantine = Quarantine()
    assert "successfully" in quarantine.summary()

    quarantine.add("x.pdf", QuarantineReason.PASSWORD_PROTECTED)
    assert quarantine.summary().startswith("1 file could not be read")


def test_every_quarantine_reason_has_advice():
    for reason in QuarantineReason:
        entry = Quarantine().add("x.pdf", reason)
        assert entry.advice
        assert entry.label


# ── The deep pass with caching ─────────────────────────────────────────


def test_deep_pass_reads_everything_and_quarantines_the_bad(mixed_folder: Path, tmp_path: Path):
    files = scan_folder(mixed_folder).drawings
    cache = CacheStore(tmp_path / "scan_cache.db")

    result = deep_inspect(files, run_id="run-1", cache=cache)

    assert result.total == len(files)
    assert not result.cancelled
    assert len(result.quarantine) >= 2  # locked, damaged, empty
    assert len(result.readable) >= 3
    cache.close()


def test_one_bad_file_never_stops_the_batch(mixed_folder: Path):
    """The whole point of the quarantine."""
    files = scan_folder(mixed_folder).drawings
    result = deep_inspect(files, run_id="run-2")

    good = {Path(info.path).name for info in result.readable}
    assert "A-101.pdf" in good
    assert "A-102-multipage.pdf" in good


def test_the_second_scan_comes_from_the_cache(mixed_folder: Path, tmp_path: Path):
    """Re-scanning an unchanged folder must do no PDF work at all."""
    files = scan_folder(mixed_folder).drawings
    cache = CacheStore(tmp_path / "scan_cache.db")

    first = deep_inspect(files, run_id="run-3", cache=cache)
    assert first.from_cache == 0

    second = deep_inspect(files, run_id="run-4", cache=cache)

    assert second.from_cache == len(first.readable)
    assert second.total == first.total
    cache.close()


def test_a_changed_file_misses_the_cache(tmp_path: Path):
    folder = tmp_path / "one"
    target = build_pdf(folder / "A-101.pdf", [SheetSpec(drawing_no="A-101")])
    cache = CacheStore(tmp_path / "cache.db")

    deep_inspect(scan_folder(folder).drawings, run_id="a", cache=cache)

    time.sleep(0.01)
    build_pdf(target, [SheetSpec(drawing_no="A-101"), SheetSpec(drawing_no="A-102")])

    second = deep_inspect(scan_folder(folder).drawings, run_id="b", cache=cache)

    assert second.from_cache == 0
    assert second.infos[0].page_count == 2
    cache.close()


def test_unreadable_files_are_not_cached(mixed_folder: Path, tmp_path: Path):
    """A file locked because someone had it open should be retried next time."""
    files = scan_folder(mixed_folder).drawings
    cache = CacheStore(tmp_path / "cache.db")

    deep_inspect(files, run_id="run-5", cache=cache)
    second = deep_inspect(files, run_id="run-6", cache=cache)

    assert second.from_cache < second.total
    cache.close()


# ── Cache store ────────────────────────────────────────────────────────


def test_cache_get_and_put(tmp_path: Path):
    cache = CacheStore(tmp_path / "c.db")
    key = CacheKey("D:\\a.pdf", 100, 1234.5)

    assert cache.get(key, INSPECT_KIND) is None
    cache.put(key, INSPECT_KIND, {"page_count": 3})
    assert cache.get(key, INSPECT_KIND) == {"page_count": 3}

    stale = CacheKey("D:\\a.pdf", 101, 1234.5)  # size changed
    assert cache.get(stale, INSPECT_KIND) is None
    cache.close()


def test_cache_prunes_files_that_no_longer_exist(tmp_path: Path):
    cache = CacheStore(tmp_path / "c.db")
    cache.put(CacheKey("D:\\gone.pdf", 1, 1.0), INSPECT_KIND, {})
    cache.put(CacheKey("D:\\here.pdf", 1, 1.0), INSPECT_KIND, {})

    removed = cache.prune_missing({"D:\\here.pdf"})

    assert removed == 1
    assert cache.count() == 1
    cache.close()


# ── Cancellation ───────────────────────────────────────────────────────


def test_cancel_stops_the_batch_quickly(tmp_path: Path):
    """A cancel button that does nothing is worse than no cancel button."""
    folder = tmp_path / "many"
    folder.mkdir()
    payload = build_pdf(folder / "seed.pdf").read_bytes()
    for index in range(120):
        (folder / f"A-{index:03d}.pdf").write_bytes(payload)

    files = scan_folder(folder).drawings
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()

    started = time.perf_counter()
    result = run_batch(
        [(item.abs_path, item.size) for item in files],
        run_id="cancel-run",
        cancel=token,
    )
    elapsed = time.perf_counter() - started

    assert result.cancelled
    assert result.completed < result.total
    assert elapsed < 8.0, f"cancel took {elapsed:.1f}s to take effect"


def test_a_token_cancelled_before_the_start_does_no_work(mixed_folder: Path):
    files = scan_folder(mixed_folder).drawings
    token = CancelToken()
    token.cancel()

    result = run_batch([(f.abs_path, f.size) for f in files], run_id="pre", cancel=token)

    assert result.cancelled
    assert result.completed == 0


def test_worker_count_leaves_headroom():
    workers = default_worker_count()
    assert workers >= 2
    import os

    assert workers <= max(2, (os.cpu_count() or 4))


# ── Progress events ────────────────────────────────────────────────────


def test_progress_events_are_coalesced_but_endings_are_not():
    """Hundreds of updates a second must not flood the UI."""
    bus = ProgressBus()
    with bus.subscribe() as subscriber:
        for index in range(500):
            bus.publish(ProgressEvent(run_id="r", stage=Stage.INSPECT, current=index, total=500))
        bus.publish(
            ProgressEvent(run_id="r", stage=Stage.INSPECT, kind=EventKind.FINISHED, current=500)
        )

        events = subscriber.drain()

    kinds = [event.kind for event in events]
    assert EventKind.FINISHED in kinds
    assert len(events) == 2  # one coalesced progress, plus the ending
    progress = next(event for event in events if event.kind is EventKind.PROGRESS)
    assert progress.current == 499  # the newest, not the oldest


def test_eta_and_fraction_are_computed():
    event = ProgressEvent(run_id="r", stage=Stage.INSPECT, current=25, total=100, elapsed=10.0)
    assert event.fraction == 0.25
    assert event.eta == pytest.approx(30.0)

    payload = event.as_dict()
    assert payload["stage"] == "inspect"
    assert payload["eta"] == pytest.approx(30.0)


def test_eta_is_none_before_there_is_anything_to_go_on():
    assert ProgressEvent(run_id="r", stage=Stage.SCAN).eta is None


def test_the_reporter_publishes_a_start_and_an_end():
    bus = ProgressBus()
    with bus.subscribe() as subscriber:
        reporter = RunReporter("r", Stage.SCAN, total=2, bus=bus)
        reporter.started()
        reporter.advance("A-101.pdf")
        reporter.advance("A-102.pdf")
        reporter.finished("done")
        events = subscriber.drain()

    kinds = [event.kind for event in events]
    assert EventKind.STARTED in kinds
    assert EventKind.FINISHED in kinds


def test_a_subscriber_stops_receiving_after_it_leaves():
    bus = ProgressBus()
    with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


# ── Durable jobs ───────────────────────────────────────────────────────


def test_jobs_can_be_queued_and_resumed(project_db):
    from engine.storage.db import session_scope
    from engine.storage.schema import Project

    with session_scope(project_db) as session:
        project = Project(name="Resume test")
        session.add(project)
        session.flush()
        project_id = project.id

    store = JobStore(project_db)
    store.enqueue_many(project_id, "inspect", [{"path": f"A-{i}.pdf"} for i in range(5)])

    assert len(store.pending(project_id)) == 5

    pending = store.pending(project_id)
    store.mark(pending[0].id, JobState.DONE)
    assert len(store.pending(project_id)) == 4


def test_jobs_left_running_by_a_crash_are_requeued(project_db):
    from engine.storage.db import session_scope
    from engine.storage.schema import Project

    with session_scope(project_db) as session:
        project = Project(name="Crash test")
        session.add(project)
        session.flush()
        project_id = project.id

    store = JobStore(project_db)
    store.enqueue_many(project_id, "inspect", [{"path": "A-1.pdf"}, {"path": "A-2.pdf"}])

    pending = store.pending(project_id)
    store.mark(pending[0].id, JobState.RUNNING)  # then the process dies

    assert store.reset_stale_running(project_id) == 1
    assert store.counts(project_id)[JobState.QUEUED] == 2


# ── Thread safety ──────────────────────────────────────────────────────


def test_pdfium_reads_survive_concurrent_threads(tmp_path: Path):
    """Regression: pdfium is not thread-safe.

    Without a lock, two threads reading different PDFs at once make pdfium
    report perfectly good drawings as "Data format error" — so the app would
    quarantine valid drawings as damaged. That is far worse than being slow,
    because it tells a quantity surveyor a real drawing is corrupt.
    """
    from engine.extract.text_extractor import extract_document_text
    from tests.fixture_builder import build_normal_pair

    old_dir, _ = build_normal_pair(tmp_path / "threads")
    files = sorted(str(item) for item in old_dir.glob("*.pdf"))
    assert files

    unreadable: list[str] = []
    empty_text: list[str] = []

    def worker() -> None:
        for _ in range(6):
            for path in files:
                if not inspect_pdf(path).is_readable:
                    unreadable.append(path)
                if not extract_document_text(path):
                    empty_text.append(path)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert unreadable == [], f"{len(unreadable)} good drawings were reported unreadable"
    assert empty_text == [], f"{len(empty_text)} good drawings returned no text"


def test_error_notes_never_leak_library_jargon(mixed_folder: Path):
    """No stack traces or library wording reach the user.

    "PDFium: Data format error" means nothing to a document controller. The
    note must say what happened and what to do next.
    """
    jargon = ("pdfium", "pikepdf", "traceback", "exception", "errno", "0x")

    for name in ("locked.pdf", "damaged.pdf", "empty.pdf"):
        note = inspect_pdf(mixed_folder / name).error_note or ""
        lowered = note.lower()

        assert note, f"{name} has no explanation"
        for word in jargon:
            assert word not in lowered, f"{name} note leaks {word!r}: {note}"

        # Every note tells the reader what to do next.
        assert any(
            hint in lowered
            for hint in ("ask the sender", "re-download", "check", "try", "scan again")
        ), f"{name} note gives no next step: {note}"
