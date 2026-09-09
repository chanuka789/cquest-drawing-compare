"""A cheap content fingerprint for one drawing sheet.

When a client changes the naming standard mid-project, nothing in the old file
name resembles the new one — tiers 1 to 4 of the matcher all fail. But the
**content** barely changed, and that is what this module measures.

The fingerprint is deliberately built from things the app can already read or
read cheaply:

* page size in millimetres (rounded to 5 mm) and orientation;
* the set of text strings on the sheet, normalised, deduplicated, **excluding
  the title block strip** so revision letters and issue dates do not colour
  the identity of the drawing;
* the number of vector paths, in a coarse bucket;
* grid bubble labels, if any, sorted.

Two versions of the same drawing typically share 85–98% of their text. Two
different drawings usually share well under 40%. That gap is what makes the
similarity score usable as the matcher's last-resort tier.

Rules that keep it honest:

* **Never call an empty sheet similar to another empty sheet.** A scanned
  drawing has no text at all; two empty fingerprints must not look alike
  because both are empty.
* **Text dominates.** Tokens weigh 0.7, structure 0.3, so a fingerprint
  comparison can never override a strong text signal in either direction.
* Fingerprints are stored, computed once per file, keyed by
  ``(path, size, mtime)`` — the same key the scan cache uses — so re-running
  a match never re-reads the PDFs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from pypdfium2 import raw as pdfium_raw

from engine.extract.text_extractor import PageText, TextItem, extract_page_text
from engine.titleblock.zone_detector import ZoneName, detect_zones
from engine.utils.pdf_runtime import open_document

POINTS_PER_MM = 72.0 / 25.4

#: Path count buckets from the plan: 0-100, 100-500, 500-2000, 2000+.
PATH_BUCKETS: tuple[int, ...] = (100, 500, 2000)

#: Weight of the text-token half of the similarity score.
TEXT_WEIGHT = 0.7

#: A token is meaningful from this many characters up (single letters and
#: digits are grid labels or dimension scraps, not identity).
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")

#: A grid bubble label: a single letter, or a small number.
_GRID_LETTER = re.compile(r"^[a-z]$")
_GRID_NUMBER = re.compile(r"^\d{1,2}$")

#: How far into the page margin a grid bubble label must sit (fraction).
_GRID_MARGIN = 0.08

_TOKEN_SEPARATOR = "\u001f"


def path_bucket(path_count: int) -> int:
    """Coarse bucket index for a path count: 0, 1, 2 or 3."""
    for index, ceiling in enumerate(PATH_BUCKETS):
        if path_count < ceiling:
            return index
    return len(PATH_BUCKETS)


@dataclass(slots=True)
class SheetFingerprint:
    """Everything identity-relevant about one sheet, computed once."""

    #: Width and height in mm, each rounded to the nearest 5 mm.
    page_size_mm: tuple[float, float]
    orientation: str  # 'landscape' | 'portrait' | 'square'
    #: Normalised, deduplicated text tokens, title block strip excluded.
    text_tokens: frozenset[str]
    #: 0 = 0-100 paths, 1 = 100-500, 2 = 500-2000, 3 = 2000+.
    path_bucket: int
    #: Sorted grid bubble labels, e.g. ``("A", "B", "1", "2")``.
    grid_labels: tuple[str, ...]
    #: Hash of the sorted token set, for fast exact comparison.
    text_hash: str

    @classmethod
    def empty(cls) -> SheetFingerprint:
        return cls(
            page_size_mm=(0.0, 0.0),
            orientation="square",
            text_tokens=frozenset(),
            path_bucket=0,
            grid_labels=(),
            text_hash=_hash_tokens(()),
        )

    def has_text_signal(self) -> bool:
        return bool(self.text_tokens or self.grid_labels)

    def as_dict(self) -> dict[str, object]:
        return {
            "page_size_mm": list(self.page_size_mm),
            "orientation": self.orientation,
            "text_tokens": sorted(self.text_tokens),
            "path_bucket": self.path_bucket,
            "grid_labels": list(self.grid_labels),
            "text_hash": self.text_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SheetFingerprint:
        size = data.get("page_size_mm", [0.0, 0.0])
        assert isinstance(size, list)
        return cls(
            page_size_mm=(float(size[0]), float(size[1])),
            orientation=str(data.get("orientation", "square")),
            text_tokens=frozenset(str(token) for token in data.get("text_tokens", [])),  # type: ignore[union-attr]
            path_bucket=int(data.get("path_bucket", 0)),
            grid_labels=tuple(str(label) for label in data.get("grid_labels", [])),  # type: ignore[union-attr]
            text_hash=str(data.get("text_hash", "")),
        )


def _hash_tokens(tokens: tuple[str, ...] | frozenset[str]) -> str:
    ordered = _TOKEN_SEPARATOR.join(sorted(tokens))
    return hashlib.sha1(ordered.encode("utf-8", errors="replace")).hexdigest()[:16]


def _size_mm(box_width_pt: float, box_height_pt: float) -> tuple[float, float]:
    width = round(box_width_pt / POINTS_PER_MM / 5) * 5
    height = round(box_height_pt / POINTS_PER_MM / 5) * 5
    return max(width, 0.0), max(height, 0.0)


def _orientation(width_mm: float, height_mm: float) -> str:
    if width_mm == height_mm:
        return "square"
    return "landscape" if width_mm > height_mm else "portrait"


def _outside_zones(items: list[TextItem], page: PageText) -> list[TextItem]:
    """Text items that sit outside the title block strip(s).

    Zone detection returns fixed strips scored by title-block vocabulary; the
    strips that score are the ones that must not colour a fingerprint, because
    revision letters and issue dates live there.
    """
    excluded: set[int] = set()
    for zone in detect_zones(page):
        if zone.name is ZoneName.WHOLE_SHEET or zone.score <= 0:
            continue
        excluded.update(id(item) for item in zone.items)
    return [item for item in items if id(item) not in excluded]


def _text_tokens(items: list[TextItem]) -> frozenset[str]:
    """Normalised word tokens from a set of text items."""
    tokens: set[str] = set()
    for item in items:
        lowered = item.clean.lower()
        tokens.update(_TOKEN_RE.findall(lowered))
    return frozenset(tokens)


def _grid_labels(items: list[TextItem], page: PageText) -> tuple[str, ...]:
    """Single letters or small numbers near the sheet edges.

    Grid bubbles sit in the margins around the drawing; a short label in the
    margin is far more likely to be an axis reference than sheet content.
    """
    labels: list[str] = []
    width, height = page.box.width, page.box.height

    def near_edge(item: TextItem) -> bool:
        margin_x = page.box.width * _GRID_MARGIN
        margin_y = page.box.height * _GRID_MARGIN
        return (
            item.x < page.box.x0 + margin_x
            or item.right > page.box.x1 - margin_x
            or item.y < page.box.y0 + margin_y
            or item.top > page.box.y1 - margin_y
        )

    for item in items:
        cleaned = item.clean.strip().lower()
        if len(cleaned) > 3 or width <= 0 or height <= 0:
            continue
        if not (_GRID_LETTER.fullmatch(cleaned) or _GRID_NUMBER.fullmatch(cleaned)):
            continue
        if near_edge(item):
            labels.append(cleaned.upper())
    return tuple(sorted(set(labels)))


def build_fingerprint(page: PageText, path_count: int) -> SheetFingerprint:
    """Fingerprint one already-extracted page.

    `path_count` comes from the PDF object walk (see
    :func:`count_paths_on_page`); it is passed in so the function stays pure
    and testable without opening a document.
    """
    if page.is_empty:
        return SheetFingerprint.empty()

    width_mm, height_mm = _size_mm(page.box.width, page.box.height)
    body_items = _outside_zones(page.items, page)
    tokens = _text_tokens(body_items)
    labels = _grid_labels(body_items, page)
    return SheetFingerprint(
        page_size_mm=(width_mm, height_mm),
        orientation=_orientation(width_mm, height_mm),
        text_tokens=tokens,
        path_bucket=path_bucket(path_count),
        grid_labels=labels,
        text_hash=_hash_tokens(tokens),
    )


def count_paths_on_page(page: pdfium.PdfPage) -> int:
    """How many vector path objects this page holds.

    pdfium merges a drawn contour into one path object, so this is closer to a
    path count than an operator count. Raised never: a page that cannot be
    walked simply counts as zero paths.
    """
    try:
        return sum(
            1 for obj in page.get_objects() if obj.type == pdfium_raw.FPDF_PAGEOBJ_PATH
        )
    except pdfium.PdfiumError:
        return 0


def build_document_fingerprints(path: str | Path) -> dict[int, SheetFingerprint]:
    """Fingerprint every page of one PDF.

    Opens the document once (pdfium is not thread-safe, so this must not run
    concurrently with other PDF work — the :func:`open_document` context holds
    the pdfium lock for its whole lifetime).
    """
    fingerprints: dict[int, SheetFingerprint] = {}
    try:
        with open_document(path) as document:
            for index in range(len(document)):
                page = document[index]
                text = extract_page_text(page, index)
                fingerprints[index] = build_fingerprint(text, count_paths_on_page(page))
    except pdfium.PdfiumError:
        pass  # an unreadable file yields no fingerprints; the matcher leaves it unmatched
    return fingerprints


def set_similarity(
    left: frozenset[str] | set[str],
    right: frozenset[str] | set[str],
    *,
    empty_agrees: bool = False,
) -> float:
    """Set similarity (Jaccard).

    `empty_agrees` is for grid labels: two sheets with no labels at all agree
    on that point. It is never used for text tokens, where an empty set on
    either side carries no evidence and must not look like a match — that is
    how two scanned (textless) sheets would otherwise pair up.
    """
    if not left and not right:
        return 1.0 if empty_agrees else 0.0
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def similarity(a: SheetFingerprint, b: SheetFingerprint) -> float:
    """0-1 similarity between two fingerprints.

    Text tokens carry 0.7 of the weight; page size, orientation, path bucket
    and grid labels share the remaining 0.3. Two versions of the same drawing
    typically score above 0.85; two different drawings usually below 0.4.

    A sheet with no text signal at all (a scan) is never similar to anything:
    two empty fingerprints must not match just because both are empty.
    """
    if not a.has_text_signal() or not b.has_text_signal():
        return 0.0
    text_score = set_similarity(a.text_tokens, b.text_tokens)
    grid_score = set_similarity(a.grid_labels, b.grid_labels, empty_agrees=True)
    size_score = 1.0 if a.page_size_mm == b.page_size_mm else 0.0
    orientation_score = 1.0 if a.orientation == b.orientation else 0.0
    path_score = 1.0 if a.path_bucket == b.path_bucket else 0.0
    structural = (size_score + orientation_score + path_score + grid_score) / 4.0
    return round(TEXT_WEIGHT * text_score + (1.0 - TEXT_WEIGHT) * structural, 4)


# ── Caching ─────────────────────────────────────────────────────────────


def fingerprint_key(path: str | Path) -> str:
    """Stable per-file identifier used when storing fingerprints."""
    return str(path).replace("\\", "/")


def page_fingerprints_payload(
    fingerprints: dict[int, SheetFingerprint],
) -> dict[str, Any]:
    """JSON-safe payload for the scan cache: one entry per page index."""
    return {"pages": {str(index): fp.as_dict() for index, fp in fingerprints.items()}}


def fingerprints_from_payload(payload: dict[str, Any]) -> dict[int, SheetFingerprint]:
    """Rebuild fingerprints from a cache payload."""
    result: dict[int, SheetFingerprint] = {}
    for index, data in payload.get("pages", {}).items():
        result[int(index)] = SheetFingerprint.from_dict(dict(data))  # type: ignore[arg-type]
    return result
