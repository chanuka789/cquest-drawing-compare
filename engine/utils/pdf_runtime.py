"""Serialised access to pdfium.

**pdfium is not thread-safe.** It keeps global state, so two threads reading
two different PDFs at the same time corrupt each other's work.

The failure mode is the dangerous kind: rather than crashing, pdfium reports
a perfectly good drawing as `Data format error`. The application would then
quarantine it as damaged, and tell a quantity surveyor that a valid drawing is
corrupt. That is far worse than being slow.

Every pdfium document is therefore opened through :func:`open_document`, which
holds a process-wide re-entrant lock for the document's whole lifetime.

This costs nothing where it matters. The deep pass gets its parallelism from
:class:`~concurrent.futures.ProcessPoolExecutor`, and each worker process has
its own pdfium and its own lock. Only threads inside one process are
serialised, which is exactly the case that was breaking.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdfium2 as pdfium

from engine.utils.longpath import long_path

#: Re-entrant, so a caller already inside `open_document` can call another
#: guarded helper on the same thread without deadlocking.
PDFIUM_LOCK = threading.RLock()


@contextmanager
def open_document(path: str | Path) -> Iterator[pdfium.PdfDocument]:
    """Open a PDF for reading, with exclusive access to pdfium.

    The document is always closed, and the lock is always released, even if
    the caller raises.
    """
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(long_path(path))
        try:
            yield document
        finally:
            document.close()


@contextmanager
def pdfium_access() -> Iterator[None]:
    """Guard a block that touches an already-open pdfium object."""
    with PDFIUM_LOCK:
        yield
