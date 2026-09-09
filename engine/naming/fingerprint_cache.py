"""Cache fingerprints in the scan cache so they are computed once per file.

Fingerprinting re-reads a PDF (text + geometry + path walk), which is exactly
the kind of work the scan cache exists to avoid repeating. Rows are keyed by
``(abs path, size, mtime)`` — the same key the deep pass uses — under the
``fingerprint.v1`` namespace, one row per file holding every page's
fingerprint. A matching run after the first is effectively free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from engine.core.models import SheetRecord
from engine.naming.fingerprint import (
    SheetFingerprint,
    build_document_fingerprints,
    fingerprints_from_payload,
    page_fingerprints_payload,
)
from engine.storage.cache_store import CacheKey, CacheStore

#: Cache namespace for per-file fingerprint payloads.
FINGERPRINT_KIND = "fingerprint.v1"


def ensure_fingerprint(
    sheet: SheetRecord, cache: CacheStore | None = None
) -> SheetFingerprint | None:
    """The cached fingerprint for one sheet, computing and storing it on a miss.

    Returns None when the file cannot be read (an unreadable sheet stays
    unmatched rather than being guessed at).
    """
    key = _key_for(sheet)
    if key is None:
        return None
    payload = cache.get(key, FINGERPRINT_KIND) if cache is not None else None
    if payload is not None:
        pages = fingerprints_from_payload(payload)
        found = pages.get(sheet.page_index)
        if found is not None:
            return found

    pages = build_document_fingerprints(sheet.abs_path)
    if not pages:
        return None
    if cache is not None and key is not None:
        try:
            cache.put(key, FINGERPRINT_KIND, page_fingerprints_payload(pages))
        except OSError:
            logger.debug("Could not write fingerprints for {}", sheet.abs_path)
    return pages.get(sheet.page_index)


def _key_for(sheet: SheetRecord) -> CacheKey | None:
    try:
        return CacheKey.for_file(sheet.abs_path)
    except OSError:
        return None


def fingerprints_memo(sheets: list[SheetRecord], cache: CacheStore | None = None) -> dict[tuple[str, int], SheetFingerprint]:
    """Fingerprints for every sheet that has one, keyed by (path, page)."""
    memo: dict[tuple[str, int], SheetFingerprint] = {}
    for sheet in sheets:
        if not sheet.is_readable:
            continue
        fingerprint = ensure_fingerprint(sheet, cache)
        if fingerprint is not None:
            memo[(sheet.abs_path, sheet.page_index)] = fingerprint
    return memo


def resolver_from_memo(
    memo: dict[tuple[str, int], SheetFingerprint],
):
    """A matcher-compatible ``fingerprint_for`` callable over a memo."""
    def resolve(sheet: SheetRecord) -> SheetFingerprint | None:
        return memo.get((sheet.abs_path, sheet.page_index))
    return resolve
