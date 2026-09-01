"""Orchestration: the steps of a run, in order, with the cache in front.

Phase 2 covers the intake half of the pipeline:

    fast scan  ->  deep inspect (cached)  ->  identify  ->  reconcile

Only the deep inspect step lives here so far. Each step is written so it can
be called on its own, because the UI runs them at different moments: the fast
scan the instant a folder is chosen, the deep pass in the background.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from engine.core.events import ProgressBus, Stage
from engine.core.jobs import BatchResult, CancelToken, run_batch
from engine.ingest.folder_scanner import ScannedFile
from engine.ingest.pdf_inspector import PdfInfo
from engine.ingest.quarantine import Quarantine
from engine.storage.cache_store import CacheKey, CacheStore

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
