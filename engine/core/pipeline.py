"""Orchestration: the steps of a run, in order, with the cache in front.

Phase 2 covers the intake half of the pipeline:

    fast scan  ->  deep inspect (cached)  ->  identify  ->  reconcile

Only the deep inspect step lives here so far. Each step is written so it can
be called on its own, because the UI runs them at different moments: the fast
scan the instant a folder is chosen, the deep pass in the background.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from engine.core.enums import Side
from engine.core.events import ProgressBus, RunReporter, Stage
from engine.core.jobs import BatchResult, CancelToken, run_batch
from engine.core.models import SheetRecord
from engine.extract.text_extractor import extract_document_text
from engine.ingest.file_hasher import content_hash
from engine.ingest.folder_scanner import ScannedFile, scan_folder
from engine.ingest.pdf_inspector import PdfInfo
from engine.ingest.quarantine import Quarantine
from engine.storage.cache_store import CacheKey, CacheStore
from engine.titleblock.field_extractor import extract_identity, normalise_number
from engine.titleblock.patterns import SheetProfile, load_profile
from engine.utils.errors import UnreadableFileError

#: Cache namespace for deep inspection results.
INSPECT_KIND = "inspect.v1"


@dataclass(slots=True)
class DeepPassResult:
    """What the deep pass learned, and how much of it came for free."""

    infos: list[PdfInfo] = field(default_factory=list)
    quarantine: Quarantine = field(default_factory=Quarantine)
    inspected: int = 0
    from_cache: int = 0
    cancelled: bool = False
    duration_seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.infos)

    @property
    def readable(self) -> list[PdfInfo]:
        return [info for info in self.infos if info.is_readable]

    def summary(self) -> str:
        if self.cancelled:
            return f"Stopped after reading {self.total} files."
        cached = f", {self.from_cache} already known" if self.from_cache else ""
        return f"Read {self.total} files in {self.duration_seconds:.1f}s{cached}."


def deep_inspect(
    files: list[ScannedFile],
    *,
    run_id: str,
    cache: CacheStore | None = None,
    cancel: CancelToken | None = None,
    bus: ProgressBus | None = None,
    max_workers: int | None = None,
    on_result: object = None,
) -> DeepPassResult:
    """Open every file in *files*, using the cache wherever it can.

    A cache hit costs one SQLite lookup, so re-scanning an unchanged folder
    does no PDF work at all.
    """
    result = DeepPassResult()
    to_inspect: list[tuple[str, int]] = []
    keys: dict[str, CacheKey] = {}

    for item in files:
        key = CacheKey(abs_path=item.abs_path, size=item.size, mtime=item.modified.timestamp())
        keys[item.abs_path] = key

        cached = cache.get(key, INSPECT_KIND) if cache is not None else None
        if cached is not None:
            result.infos.append(PdfInfo.from_dict(cached))
            result.from_cache += 1
        else:
            to_inspect.append((item.abs_path, item.size))

    logger.info(
        "Deep pass | {} files | {} cached | {} to read",
        len(files),
        result.from_cache,
        len(to_inspect),
    )

    batch: BatchResult | None = None
    if to_inspect:
        batch = run_batch(
            to_inspect,
            run_id=run_id,
            stage=Stage.INSPECT,
            max_workers=max_workers,
            cancel=cancel,
            bus=bus,
            on_result=on_result,  # type: ignore[arg-type]
        )
        result.cancelled = batch.cancelled
        result.duration_seconds = batch.duration_seconds
        result.inspected = batch.completed

        fresh: list[tuple[CacheKey, str, dict[str, Any]]] = []
        for payload in batch.results:
            info = PdfInfo.from_dict(payload)
            result.infos.append(info)
            key = keys.get(info.path)
            # Only cache a file we could actually read. A file that was locked
            # because someone had it open should be retried next time.
            if cache is not None and key is not None and info.is_readable:
                fresh.append((key, INSPECT_KIND, payload))

        if cache is not None and fresh:
            cache.put_many(fresh)

    for info in result.infos:
        result.quarantine.add_from_inspection(info)

    # Keep the order stable for display, whatever order the pool finished in.
    result.infos.sort(key=lambda info: info.path.lower())
    return result


# ── Identifying the sheets ─────────────────────────────────────────────


def identify_sheets(
    files: list[ScannedFile],
    infos: list[PdfInfo],
    *,
    side: Side,
    profile: SheetProfile | None = None,
    hash_content: bool = True,
    run_id: str | None = None,
    bus: ProgressBus | None = None,
) -> list[SheetRecord]:
    """Turn inspected files into sheet records, one per page.

    A file is not a drawing: one PDF can hold twenty of them. The unit of work
    from here on is the sheet, which is why this expands multi-page files.

    This reports its own progress. Reading every title block takes longer than
    the inspection pass that precedes it, so without a stage of its own the
    progress rail would sit at "finished" while the slower half was still
    running, and the UI would never learn that the sheets had arrived.
    """
    profile = profile or load_profile()
    by_path = {item.abs_path: item for item in files}
    records: list[SheetRecord] = []

    reporter = (
        RunReporter(run_id, Stage.EXTRACT, total=len(infos), bus=bus)
        if run_id is not None
        else None
    )
    if reporter is not None:
        reporter.started()

    for info in infos:
        scanned = by_path.get(info.path)
        filename = scanned.filename if scanned else Path(info.path).name
        rel_path = scanned.rel_path if scanned else filename

        if not info.is_readable:
            records.append(
                SheetRecord(
                    side=side,
                    abs_path=info.path,
                    rel_path=rel_path,
                    filename=filename,
                    size=info.size,
                    is_readable=False,
                    error_note=info.error_note,
                )
            )
            if reporter is not None:
                reporter.advance(filename)
            continue

        # Hashing is what separates `unchanged` from `same_rev_different_file`,
        # so it is worth the read.
        file_hash: str | None = None
        if hash_content:
            try:
                file_hash = content_hash(info.path)
            except UnreadableFileError:
                file_hash = None

        page_text = extract_document_text(info.path)
        page_records: list[SheetRecord] = []

        for page in info.pages:
            text = page_text.get(page.index)
            identity = (
                extract_identity(
                    text,
                    filename=filename,
                    profile=profile,
                    sheet_size=page.sheet_size,
                    # A file holding many drawings is named for the issue, not
                    # for any one sheet in it.
                    filename_names_the_sheet=info.page_count == 1,
                )
                if text is not None
                else None
            )

            record = SheetRecord(
                side=side,
                abs_path=info.path,
                rel_path=rel_path,
                filename=filename,
                page_index=page.index,
                page_count=info.page_count,
                size=info.size,
                content_hash=_page_identity_hash(file_hash, page.index, info.page_count),
                sheet_size=page.sheet_size,
                looks_scanned=page.looks_scanned,
                is_readable=True,
            )

            if identity is not None:
                record.drawing_no = identity.drawing_no
                record.normalised_no = normalise_number(identity.drawing_no)
                record.source_of_number = str(identity.source_of_number)
                record.number_confidence = identity.number_confidence
                record.number_mismatch = identity.number_mismatch
                record.filename_number = identity.filename_number
                record.title = identity.title
                record.revision = identity.revision
                record.scale = identity.scale
                record.warnings = list(identity.warnings)
            else:
                record.warnings = ["This sheet has no readable text layer."]

            page_records.append(record)

        records.extend(_collapse_multi_sheet(page_records))

        if reporter is not None:
            reporter.advance(filename)

    if reporter is not None:
        reporter.finished(f"Read {len(records)} drawings.")

    return records


def _collapse_multi_sheet(page_records: list[SheetRecord]) -> list[SheetRecord]:
    """Collapse the pages of one file that all carry the same drawing number.

    One file is not one drawing, and neither is one page. A 30-page issue PDF
    holds 30 different drawings, but a drawing issued as "Sheet 1 of 3" is a
    single drawing spread over three pages.

    Without this, the second and third sheets look like the same drawing
    appearing twice in one folder, and the register reports them as
    "superseded in folder" -- telling the user that current sheets have been
    superseded, which is simply untrue.
    """
    if len(page_records) < 2:
        return page_records

    identified = [record for record in page_records if record.identified]
    if len(identified) != len(page_records):
        # Some pages had no number of their own. Keep every page, so nothing
        # is silently dropped and the user can resolve them one by one.
        return page_records

    numbers = {record.normalised_no for record in identified}
    if len(numbers) > 1:
        return page_records  # a multi-drawing file: each page is its own sheet

    first = page_records[0]
    first.sheets_in_drawing = len(page_records)
    return [first]


def _page_identity_hash(file_hash: str | None, page_index: int, page_count: int) -> str | None:
    """Content identity for one sheet.

    For a single-page file the file hash is the sheet hash. For a multi-page
    file the page index is folded in, so two sheets from the same PDF are not
    mistaken for duplicates of each other.
    """
    if file_hash is None:
        return None
    if page_count <= 1:
        return file_hash
    return f"{file_hash}:{page_index}"


def intake_folder(
    folder: str,
    *,
    side: Side,
    run_id: str,
    cache: CacheStore | None = None,
    cancel: CancelToken | None = None,
    profile: SheetProfile | None = None,
    bus: ProgressBus | None = None,
) -> tuple[list[SheetRecord], DeepPassResult]:
    """Scan, inspect and identify one issue folder, start to finish."""
    scan = scan_folder(folder)
    deep = deep_inspect(scan.drawings, run_id=run_id, cache=cache, cancel=cancel, bus=bus)
    sheets = identify_sheets(
        scan.drawings, deep.infos, side=side, profile=profile, run_id=run_id, bus=bus
    )

    logger.info(
        "Intake {} | {} files -> {} sheets ({} identified)",
        folder,
        len(scan.drawings),
        len(sheets),
        sum(1 for sheet in sheets if sheet.identified),
    )
    return sheets, deep
