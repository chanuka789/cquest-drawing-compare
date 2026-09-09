"""Progress events, from wherever the work happens to the WebSocket.

Work runs on background threads and in a process pool; the WebSocket lives on
the API event loop. This module is the thread-safe bridge between them.

Two rules shape the design:

* **Never flood the UI.** A 300-file deep pass emits hundreds of events per
  second. Progress events for the same run are *coalesced*: a subscriber only
  ever sees the most recent one. The plan's ceiling of ten updates per second
  is enforced by the reader, which polls at 100 ms.
* **Never drop an ending.** Started, finished, failed and cancelled events are
  queued rather than coalesced, because losing one would leave a progress rail
  spinning forever.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

#: How often the WebSocket reader wakes up. Ten updates per second.
POLL_INTERVAL_SECONDS = 0.1


class EventKind(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(StrEnum):
    """Which part of the run this event belongs to."""

    SCAN = "scan"  # fast pass
    INSPECT = "inspect"  # deep pass
    EXTRACT = "extract"  # title block
    RECONCILE = "reconcile"
    MATCH = "match"  # Phase 3: pairing sheets
    FINGERPRINT = "fingerprint"  # Phase 3: computing sheet fingerprints
    RENAME = "rename"  # Phase 3: applying renames
    UNDO = "undo"  # Phase 3: reversing renames
    EXPORT = "export"


@dataclass(slots=True)
class ProgressEvent:
    """One update about a run. Shaped for direct display."""

    run_id: str
    stage: Stage
    kind: EventKind = EventKind.PROGRESS
    current: int = 0
    total: int = 0
    #: The file being worked on right now. Users trust named progress.
    current_item: str = ""
    elapsed: float = 0.0
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fraction(self) -> float:
        return self.current / self.total if self.total else 0.0

    @property
    def eta(self) -> float | None:
        """Seconds remaining, or None while there is nothing to go on."""
        if self.current <= 0 or self.total <= 0 or self.elapsed <= 0:
            return None
        rate = self.current / self.elapsed
        if rate <= 0:
            return None
        return max(0.0, (self.total - self.current) / rate)

    @property
    def is_terminal(self) -> bool:
        return self.kind in {EventKind.FINISHED, EventKind.FAILED, EventKind.CANCELLED}

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stage"] = str(self.stage)
        data["kind"] = str(self.kind)
        data["fraction"] = round(self.fraction, 4)
        data["eta"] = self.eta
        return data


class Subscriber:
    """One reader's view of the bus, with coalescing built in."""

    __slots__ = ("_latest", "_lock", "_terminal")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[tuple[str, Stage], ProgressEvent] = {}
        self._terminal: deque[ProgressEvent] = deque(maxlen=256)

    def offer(self, event: ProgressEvent) -> None:
        with self._lock:
            if event.is_terminal or event.kind is EventKind.STARTED:
                self._terminal.append(event)
            else:
                # Replace, do not append: only the newest progress matters.
                self._latest[(event.run_id, event.stage)] = event

    def drain(self) -> list[ProgressEvent]:
        """Take everything waiting. Endings first, then one update per run."""
        with self._lock:
            events = list(self._terminal)
            self._terminal.clear()
            events.extend(self._latest.values())
            self._latest.clear()
        return events


class ProgressBus:
    """Fan-out of progress events to every connected reader."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[Subscriber] = set()

    def publish(self, event: ProgressEvent) -> None:
        """Safe to call from any thread, including a pool callback."""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(event)

    @contextmanager
    def subscribe(self) -> Iterator[Subscriber]:
        subscriber = Subscriber()
        with self._lock:
            self._subscribers.add(subscriber)
        try:
            yield subscriber
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


#: One bus per process. The engine runs in a single process.
progress_bus = ProgressBus()

#: Set when the engine is shutting down.
#:
#: Long-lived readers (the progress WebSocket) poll this and return, so
#: Uvicorn's graceful shutdown is not left waiting on a socket that would
#: otherwise loop forever. Without it, closing the window took the full
#: shutdown timeout before the process would exit.
shutdown_requested = threading.Event()


class RunReporter:
    """Convenience wrapper so callers do not build events by hand."""

    def __init__(
        self,
        run_id: str,
        stage: Stage,
        total: int = 0,
        bus: ProgressBus | None = None,
    ) -> None:
        self.run_id = run_id
        self.stage = stage
        self.total = total
        self._bus = bus or progress_bus
        self._started = time.monotonic()
        self._current = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def started(self, message: str = "") -> None:
        logger.info("Run {} | {} started | total={}", self.run_id, self.stage, self.total)
        self._publish(EventKind.STARTED, message=message)

    def advance(self, item: str = "", step: int = 1) -> None:
        self._current += step
        self._publish(EventKind.PROGRESS, current_item=item)

    def set_total(self, total: int) -> None:
        self.total = total

    def finished(self, message: str = "") -> None:
        logger.info("Run {} | {} finished in {:.2f}s", self.run_id, self.stage, self.elapsed)
        self._publish(EventKind.FINISHED, message=message)

    def failed(self, message: str) -> None:
        logger.error("Run {} | {} failed: {}", self.run_id, self.stage, message)
        self._publish(EventKind.FAILED, message=message)

    def cancelled(self, message: str = "Cancelled.") -> None:
        logger.info("Run {} | {} cancelled after {:.2f}s", self.run_id, self.stage, self.elapsed)
        self._publish(EventKind.CANCELLED, message=message)

    def _publish(self, kind: EventKind, current_item: str = "", message: str = "") -> None:
        self._bus.publish(
            ProgressEvent(
                run_id=self.run_id,
                stage=self.stage,
                kind=kind,
                current=self._current,
                total=self.total,
                current_item=current_item,
                elapsed=self.elapsed,
                message=message,
            )
        )
