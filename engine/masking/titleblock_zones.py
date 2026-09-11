"""Task 5.2 — title block, revision table and frame zones.

The title block is the one region on a construction drawing that is
*guaranteed* to differ between two issues: the revision letter, the date and
usually a signature change every single time. Comparing it produces a change
on every sheet of every project, which is the fastest possible way to teach a
user that the tool is noise.

Detection runs on two independent kinds of evidence and says which it used:

* **Lines.** Long straight lines near the right and bottom edges enclose
  rectangular regions. This is what a title block actually is.
* **Labels.** The region carrying two or more of ``DRAWING NO``, ``SCALE``,
  ``DRAWN``, ``CHECKED``, ``DATE``, ``CLIENT`` is the title block. Labels
  alone are enough when the sheet is text-only; lines alone are never enough,
  because a sheet frame encloses plenty of rectangles that are not title
  blocks.

The revision history table gets its own treatment: its **content is read and
kept** — it frequently states in words what changed, which is the most useful
sentence on the sheet — and its **pixels are never diffed**.

Four regions are explicitly protected from masking, because a change in them
is real and expensive: the north arrow, the scale bar, the key plan and the
general notes. They sit near the sheet edge and a slightly over-wide title
block detection would otherwise swallow them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from loguru import logger

from engine.extract.text_extractor import TextItem
from engine.masking.types import (
    FracRect,
    ProtectedRegion,
    ProtectedType,
    SheetView,
    Zone,
    ZoneType,
)

#: Two or more of these in one region make it the title block.
TITLE_BLOCK_LABELS: tuple[str, ...] = (
    "DRAWING NO",
    "DRAWING NUMBER",
    "DWG NO",
    "DRG NO",
    "SHEET NO",
    "SHEET NUMBER",
    "SCALE",
    "DRAWN",
    "CHECKED",
    "APPROVED",
    "DATE",
    "PROJECT",
    "CLIENT",
    "REV",
    "REVISION",
    "TITLE",
    "DESIGNED",
)

#: Sub-zones inside the title block, keyed by the label that finds them.
SUB_ZONE_LABELS: dict[str, tuple[str, ...]] = {
    "revision": ("REV", "REVISION", "REV."),
    "date": ("DATE", "ISSUE DATE"),
    "drawn_by": ("DRAWN", "DRAWN BY", "DESIGNED"),
    "checked_by": ("CHECKED", "CHKD", "APPROVED", "AUTHORISED"),
    "sheet_number": ("DRAWING NO", "DWG NO", "DRG NO", "SHEET NO"),
    "scale": ("SCALE",),
}

#: A revision history table's header row carries these.
REVISION_TABLE_HEADERS: tuple[str, ...] = (
    "REV",
    "REVISION",
    "DATE",
    "DESCRIPTION",
    "AMENDMENT",
    "AMENDMENTS",
    "ISSUE",
    "BY",
    "CHK",
    "APP",
)

#: Keywords that mark a region as protected rather than masked.
PROTECTED_KEYWORDS: dict[ProtectedType, tuple[str, ...]] = {
    ProtectedType.NORTH_ARROW: ("NORTH", "TRUE NORTH", "PROJECT NORTH"),
    ProtectedType.SCALE_BAR: ("SCALE BAR", "GRAPHIC SCALE", "METRES", "METERS"),
    ProtectedType.KEY_PLAN: ("KEY PLAN", "LOCATION PLAN", "SITE KEY", "KEY DIAGRAM"),
    ProtectedType.GENERAL_NOTES: (
        "GENERAL NOTES",
        "NOTES",
        "NOTE:",
        "NOTES:",
        "SPECIFICATION NOTES",
    ),
}

#: A line must span this fraction of the page dimension to count as structural.
MIN_LINE_FRACTION = 0.30
#: Hough runs on a downsampled copy; full A1 at 200 DPI is far too slow.
HOUGH_MAX_WIDTH = 1600
#: How close to the page edge a line must be to count as a frame line.
FRAME_EDGE_FRACTION = 0.08
#: Half-width of the band masked around each frame line, in page fractions.
FRAME_BAND = 0.005
#: A title block edge only snaps to a ruled line this close to it. Without
#: the limit, a sheet whose only long verticals are its own border snaps the
#: block out to the full width of the page and masks the scale bar with it.
MAX_SNAP_FRACTION = 0.12


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).upper().split())


@dataclass(slots=True)
class LineSet:
    """Long straight lines on a sheet, as page fractions."""

    #: y positions of near-horizontal lines, 0 at the top.
    horizontals: list[float] = field(default_factory=list)
    #: x positions of near-vertical lines.
    verticals: list[float] = field(default_factory=list)
    found: bool = False

    def nearest_below(self, values: list[float], value: float) -> float | None:
        """The smallest entry greater than *value*."""
        candidates = [item for item in values if item > value + 1e-6]
        return min(candidates) if candidates else None

    def nearest_above(self, values: list[float], value: float) -> float | None:
        candidates = [item for item in values if item < value - 1e-6]
        return max(candidates) if candidates else None


def find_long_lines(gray: np.ndarray | None, min_fraction: float = MIN_LINE_FRACTION) -> LineSet:
    """Near-horizontal and near-vertical lines spanning *min_fraction* of the page.

    Returns positions as page fractions so the result is independent of DPI.
    A sheet with no image (text-only detection) returns an empty set rather
    than an error — label evidence alone still detects a title block.
    """
    if gray is None or gray.size == 0:
        return LineSet()

    height, width = gray.shape[:2]
    scale = min(1.0, HOUGH_MAX_WIDTH / max(width, 1))
    small = (
        cv2.resize(gray, (max(1, int(width * scale)), max(1, int(height * scale))))
        if scale < 1.0
        else gray
    )
    small_height, small_width = small.shape[:2]

    ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    min_length = int(min_fraction * min(small_width, small_height))
    segments = cv2.HoughLinesP(
        ink,
        rho=1,
        theta=np.pi / 360,
        threshold=max(40, min_length // 3),
        minLineLength=max(20, min_length),
        maxLineGap=max(3, min_length // 20),
    )
    if segments is None:
        return LineSet(found=False)

    horizontals: list[float] = []
    verticals: list[float] = []
    # OpenCV 4 returns (N, 1, 4); OpenCV 5 returns (N, 4).
    for x1, y1, x2, y2 in np.asarray(segments).reshape(-1, 4):
        dx, dy = abs(int(x2) - int(x1)), abs(int(y2) - int(y1))
        if dy <= max(2, dx * 0.02) and dx >= min_length:
            horizontals.append((y1 + y2) / 2.0 / small_height)
        elif dx <= max(2, dy * 0.02) and dy >= min_length:
            verticals.append((x1 + x2) / 2.0 / small_width)

    return LineSet(
        horizontals=_cluster_positions(horizontals),
        verticals=_cluster_positions(verticals),
        found=bool(horizontals or verticals),
    )


def _cluster_positions(values: list[float], tolerance: float = 0.004) -> list[float]:
    """Collapse near-duplicate line positions into one each."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


# ── Label evidence ──────────────────────────────────────────────────────


@dataclass(slots=True)
class LabelHit:
    """One title-block label found on the sheet."""

    keyword: str
    item: TextItem
    rect: FracRect


def _label_hits(sheet: SheetView, keywords: tuple[str, ...]) -> list[LabelHit]:
    page = sheet.page_text
    if page is None:
        return []
    hits: list[LabelHit] = []
    for item in page.items:
        text = _normalise(item.clean)
        if not text or len(text) > 40:
            continue
        for keyword in keywords:
            if text == keyword or text.startswith(keyword) or text.rstrip(".:") == keyword:
                hits.append(LabelHit(keyword, item, sheet.item_rect(item)))
                break
    return hits


def _cluster_hits(hits: list[LabelHit], radius: float = 0.22) -> list[list[LabelHit]]:
    """Single-link clustering of label hits by fractional distance."""
    remaining = list(hits)
    clusters: list[list[LabelHit]] = []
    while remaining:
        seed = remaining.pop()
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                for member in cluster:
                    cx, cy = candidate.rect.centre
                    mx, my = member.rect.centre
                    if abs(cx - mx) <= radius and abs(cy - my) <= radius:
                        cluster.append(candidate)
                        remaining.remove(candidate)
                        changed = True
                        break
        clusters.append(cluster)
    return clusters


def _edge_score(rect: FracRect) -> float:
    """How close a region sits to the bottom-right corner. 1.0 is on it."""
    cx, cy = rect.centre
    return (cx + cy) / 2.0


# ── Detection ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class RevisionRow:
    """One parsed row of the revision history table."""

    revision: str = ""
    date: str = ""
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"revision": self.revision, "date": self.date, "description": self.description}


@dataclass(slots=True)
class DetectedZones:
    """Everything one sheet's zone detection produced."""

    zones: list[Zone] = field(default_factory=list)
    protected: list[ProtectedRegion] = field(default_factory=list)
    #: The revision table's content — read, never diffed.
    revision_rows: list[RevisionRow] = field(default_factory=list)
    lines: LineSet = field(default_factory=LineSet)
    note: str = ""

    def of_type(self, zone_type: ZoneType) -> list[Zone]:
        return [zone for zone in self.zones if zone.type is zone_type]

    @property
    def titleblock(self) -> Zone | None:
        found = self.of_type(ZoneType.TITLEBLOCK)
        return found[0] if found else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "zones": [zone.as_dict() for zone in self.zones],
            "protected": [region.as_dict() for region in self.protected],
            "revision_rows": [row.as_dict() for row in self.revision_rows],
            "note": self.note,
        }


def detect_zones(sheet: SheetView) -> DetectedZones:
    """Find the title block, its sub-zones, the revision table and the frame.

    Every zone carries a confidence and the evidence behind it, so the mask
    editor can say *why* a region was excluded rather than presenting an
    unexplained grey rectangle.
    """
    lines = find_long_lines(sheet.gray)
    result = DetectedZones(lines=lines)

    result.zones.extend(_detect_frame(sheet, lines))

    titleblock = _detect_titleblock(sheet, lines)
    if titleblock is not None:
        result.zones.append(titleblock)
        result.zones.extend(_detect_sub_zones(sheet, titleblock))

    table_zone, rows = _detect_revision_table(sheet, titleblock)
    if table_zone is not None:
        result.zones.append(table_zone)
        result.revision_rows = rows

    result.protected = detect_protected_regions(
        sheet, titleblock.rect if titleblock is not None else None
    )

    if titleblock is None:
        result.note = (
            "No title block was found on this sheet, so nothing was excluded from "
            "the comparison. Draw an exclusion zone if the revision panel shows up "
            "as a change."
        )
    logger.debug(
        "Zone detection | zones={} | protected={} | revision_rows={}",
        len(result.zones),
        len(result.protected),
        len(result.revision_rows),
    )
    return result


def _detect_frame(sheet: SheetView, lines: LineSet) -> list[Zone]:
    """The outer sheet border: identical every issue, pure noise.

    The frame is masked as four thin bands, not as the rectangle they
    enclose. Masking the rectangle would mask the entire drawing — which is
    exactly what the first version of this function did, and every sheet
    compared clean because nothing at all was compared.
    """
    if not lines.found:
        return []
    left = [value for value in lines.verticals if value <= FRAME_EDGE_FRACTION]
    right = [value for value in lines.verticals if value >= 1.0 - FRAME_EDGE_FRACTION]
    top = [value for value in lines.horizontals if value <= FRAME_EDGE_FRACTION]
    bottom = [value for value in lines.horizontals if value >= 1.0 - FRAME_EDGE_FRACTION]
    if not (left and right and top and bottom):
        return []

    x0, x1 = min(left), max(right)
    y0, y1 = min(top), max(bottom)
    evidence = [
        f"Border lines found on all four edges "
        f"({len(left)} left, {len(right)} right, {len(top)} top, {len(bottom)} bottom). "
        "Only the border bands themselves are excluded."
    ]
    bands = (
        FracRect(0.0, 0.0, 1.0, y0 + FRAME_BAND),
        FracRect(0.0, y1 - FRAME_BAND, 1.0, 1.0),
        FracRect(0.0, 0.0, x0 + FRAME_BAND, 1.0),
        FracRect(x1 - FRAME_BAND, 0.0, 1.0, 1.0),
    )
    return [
        Zone(
            type=ZoneType.FRAME,
            rect=band.clipped(),
            label="Sheet frame",
            confidence=0.9,
            evidence=evidence,
        )
        for band in bands
    ]


def _detect_titleblock(sheet: SheetView, lines: LineSet) -> Zone | None:
    hits = _label_hits(sheet, TITLE_BLOCK_LABELS)
    if not hits:
        return None

    clusters = _cluster_hits(hits)
    scored: list[tuple[float, list[LabelHit]]] = []
    for cluster in clusters:
        keywords = {hit.keyword for hit in cluster}
        if len(keywords) < 2:
            continue
        rect = FracRect.from_points(
            [(hit.rect.x0, hit.rect.y0) for hit in cluster]
            + [(hit.rect.x1, hit.rect.y1) for hit in cluster]
        )
        # Title blocks live at the bottom or right of a sheet. A cluster in
        # the middle of the drawing is a schedule, not a title block.
        scored.append((len(keywords) + 2.0 * _edge_score(rect), cluster))

    if not scored:
        return None
    scored.sort(key=lambda entry: entry[0], reverse=True)
    cluster = scored[0][1]
    keywords = sorted({hit.keyword for hit in cluster})

    rect = FracRect.from_points(
        [(hit.rect.x0, hit.rect.y0) for hit in cluster]
        + [(hit.rect.x1, hit.rect.y1) for hit in cluster]
    )
    # Values sit below and to the right of their labels, so the text cluster
    # under-reports the block. Grow it to the enclosing structural lines.
    padded = rect.expanded(0.012)
    evidence = [f"Found the labels {', '.join(keywords)} together in one region."]

    def snap(candidate: float | None, text_edge: float, fallback: float) -> float:
        """Take the ruled line only when it is genuinely beside the text."""
        if candidate is None or abs(candidate - text_edge) > MAX_SNAP_FRACTION:
            return fallback
        return candidate

    # The near edges fall back to the text itself; the far edges run to the
    # sheet edge, which is where a title block always ends.
    snapped = FracRect(
        snap(lines.nearest_above(lines.verticals, padded.x0), padded.x0, padded.x0),
        snap(lines.nearest_above(lines.horizontals, padded.y0), padded.y0, padded.y0),
        snap(lines.nearest_below(lines.verticals, padded.x1), padded.x1, 1.0),
        snap(lines.nearest_below(lines.horizontals, padded.y1), padded.y1, 1.0),
    )
    if lines.found and snapped != padded and snapped.area <= 0.45:
        padded = snapped
        evidence.append("Snapped to the ruled lines that enclose the block.")

    confidence = min(0.98, 0.5 + 0.12 * len(keywords))
    if lines.found:
        confidence = min(0.99, confidence + 0.1)

    return Zone(
        type=ZoneType.TITLEBLOCK,
        rect=padded.clipped(),
        label="Title block",
        confidence=confidence,
        evidence=evidence,
    )


def _detect_sub_zones(sheet: SheetView, titleblock: Zone) -> list[Zone]:
    """Revision, date, drawn-by, checked-by, sheet number and scale fields.

    A title block value sits below its label as often as to the right — the
    fixture's ``Drawing No.`` value is below, its ``Scale`` value is to the
    right — so a sub-zone covers the label plus a band in both directions.
    """
    page = sheet.page_text
    if page is None:
        return []
    zones: list[Zone] = []
    for name, keywords in SUB_ZONE_LABELS.items():
        for hit in _label_hits(sheet, keywords):
            if not titleblock.rect.contains(*hit.rect.centre):
                continue
            rect = FracRect(
                hit.rect.x0 - 0.004,
                hit.rect.y0 - 0.004,
                min(hit.rect.x1 + 0.09, titleblock.rect.x1),
                min(hit.rect.y1 + 0.035, titleblock.rect.y1),
            )
            zones.append(
                Zone(
                    type=ZoneType.TITLEBLOCK,
                    rect=rect.clipped(),
                    label=f"Title block: {name.replace('_', ' ')}",
                    confidence=0.85,
                    evidence=[f"Label '{hit.keyword}' with the field below and to its right."],
                )
            )
            break
    return zones


_DATE_PATTERN = re.compile(
    r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*\d{2,4})\b",
    re.IGNORECASE,
)
_REV_PATTERN = re.compile(r"^(P?\d{1,2}|[A-Z]\d?|C\d{2})$")


def _detect_revision_table(
    sheet: SheetView, titleblock: Zone | None
) -> tuple[Zone | None, list[RevisionRow]]:
    """The amendment table: masked from the diff, but its text is kept."""
    page = sheet.page_text
    if page is None:
        return None, []

    headers = [
        hit
        for hit in _label_hits(sheet, REVISION_TABLE_HEADERS)
        if hit.keyword in {"DESCRIPTION", "AMENDMENT", "AMENDMENTS", "REVISION", "ISSUE"}
    ]
    if not headers:
        return None, []

    # Prefer a header row near the title block: that is where the amendment
    # table lives. A lone DESCRIPTION in the middle of the sheet is a legend.
    if titleblock is not None:
        near = [
            hit
            for hit in headers
            if abs(hit.rect.centre[1] - titleblock.rect.centre[1]) < 0.35
            or abs(hit.rect.centre[0] - titleblock.rect.centre[0]) < 0.35
        ]
        headers = near or headers

    anchor = max(headers, key=lambda hit: _edge_score(hit.rect))
    row_height = max(anchor.rect.height, 0.008)
    band = FracRect(
        min(anchor.rect.x0 - 0.10, 1.0),
        anchor.rect.y0 - row_height * 1.5,
        min(anchor.rect.x1 + 0.22, 1.0),
        anchor.rect.y1 + row_height * 10.0,
    ).clipped()

    inside = [item for item in page.items if band.contains(*sheet.item_centre(item))]
    if len(inside) < 2:
        return None, []

    rect = FracRect.from_points(
        [(sheet.item_rect(item).x0, sheet.item_rect(item).y0) for item in inside]
        + [(sheet.item_rect(item).x1, sheet.item_rect(item).y1) for item in inside]
    )
    zone = Zone(
        type=ZoneType.REVISION_TABLE,
        rect=rect.clipped(),
        label="Revision history",
        confidence=0.8,
        evidence=[
            f"Header '{anchor.keyword}' with {len(inside)} entries below it. "
            "Its text is read and kept; its pixels are not compared."
        ],
    )
    return zone, parse_revision_rows(sheet, inside)


def parse_revision_rows(sheet: SheetView, items: list[TextItem]) -> list[RevisionRow]:
    """Group the table's text into rows of revision, date and description.

    The table often says in words what changed on the sheet, which is the
    single most useful sentence on it. Phase 6 cross-checks it against the
    changes found; Phase 5's job is only to keep it.
    """
    if not items:
        return []
    entries = [(sheet.item_rect(item), item) for item in items]
    entries.sort(key=lambda entry: entry[0].centre[1])

    rows: list[list[tuple[FracRect, TextItem]]] = []
    tolerance = max(0.006, np.median([rect.height for rect, _ in entries]) * 0.8)
    for rect, item in entries:
        if rows and abs(rows[-1][-1][0].centre[1] - rect.centre[1]) <= tolerance:
            rows[-1].append((rect, item))
        else:
            rows.append([(rect, item)])

    parsed: list[RevisionRow] = []
    for row in rows:
        row.sort(key=lambda entry: entry[0].x0)
        texts = [entry[1].clean for entry in row]
        header_like = {_normalise(text) for text in texts} & set(REVISION_TABLE_HEADERS)
        if len(header_like) >= 2:
            continue  # the header row itself
        revision = ""
        date = ""
        description_parts: list[str] = []
        for text in texts:
            normalised = _normalise(text)
            if not revision and _REV_PATTERN.match(normalised):
                revision = text
                continue
            if not date and _DATE_PATTERN.search(text):
                date = text
                continue
            description_parts.append(text)
        description = " ".join(part for part in description_parts if part)
        if revision or date or description:
            parsed.append(
                RevisionRow(revision=revision, date=date, description=description.strip())
            )
    return parsed


def detect_protected_regions(
    sheet: SheetView, titleblock: FracRect | None = None
) -> list[ProtectedRegion]:
    """North arrow, scale bar, key plan and general notes — never masked.

    A rotated north arrow is a real and significant change. A changed scale
    bar means the drawing scale changed. A changed general note is often the
    most expensive change on the sheet. All three sit close enough to the
    sheet edge that a generous title block detection would eat them.

    ``titleblock`` clips every protected region away from it. Protection wins
    over masking, so a notes column that reached into the title block would
    un-mask the revision letter and the date — and then every sheet in the
    project reports them as changes, which is the failure this whole module
    exists to prevent.
    """
    page = sheet.page_text
    if page is None:
        return []

    regions: list[ProtectedRegion] = []
    for kind, keywords in PROTECTED_KEYWORDS.items():
        for hit in _label_hits(sheet, keywords):
            rect = _clip_away_from(_protected_extent(sheet, kind, hit.rect), titleblock)
            if rect is None:
                continue
            regions.append(
                ProtectedRegion(
                    type=kind,
                    rect=rect.clipped(),
                    label=kind.replace("_", " ").title(),
                    confidence=0.8,
                    evidence=[f"Found '{hit.keyword}' here, so this region is still compared."],
                )
            )
            break

    for region in _north_arrow_regions(sheet):
        rect = _clip_away_from(region.rect, titleblock)
        if rect is not None:
            region.rect = rect
            regions.append(region)
    return regions


def _clip_away_from(rect: FracRect, block: FracRect | None) -> FracRect | None:
    """Pull *rect* back so it stops before *block*, or drop it if it cannot."""
    if block is None or not rect.intersects(block):
        return rect
    if rect.y0 < block.y0:
        return FracRect(rect.x0, rect.y0, rect.x1, block.y0)
    if rect.x0 < block.x0:
        return FracRect(rect.x0, rect.y0, block.x0, rect.y1)
    return None  # entirely inside the title block: it is title block text


#: A gap larger than this many line heights ends the notes column.
NOTES_GAP_LINES = 3.0
#: And the column never runs further than this down the sheet.
NOTES_MAX_REACH = 0.45


def _protected_extent(sheet: SheetView, kind: ProtectedType, anchor: FracRect) -> FracRect:
    """How far a protected region reaches beyond its keyword."""
    if kind is ProtectedType.GENERAL_NOTES:
        return _notes_column(sheet, anchor)
    if kind is ProtectedType.KEY_PLAN:
        return anchor.expanded(0.09)
    return anchor.expanded(0.05)


def _notes_column(sheet: SheetView, anchor: FracRect) -> FracRect:
    """The block of notes under a heading: same column, no large gaps.

    Reading to a fixed distance down the page walks straight out of the note
    block and into whatever is below it. Stopping at the first gap of a few
    line heights is what a reader does, and it is what the block is.
    """
    page = sheet.page_text
    rect = anchor
    if page is None:
        return rect.expanded(0.01)

    line_height = max(anchor.height, 0.004)
    column = sorted(
        (
            sheet.item_rect(item)
            for item in page.items
            if anchor.x0 - 0.02 <= sheet.item_rect(item).x0 <= anchor.x0 + 0.30
            and anchor.y1 <= sheet.item_rect(item).centre[1] <= anchor.y1 + NOTES_MAX_REACH
        ),
        key=lambda item: item.y0,
    )

    bottom = anchor.y1
    for item_rect in column:
        if item_rect.y0 - bottom > line_height * NOTES_GAP_LINES:
            break
        rect = rect.union(item_rect)
        bottom = max(bottom, item_rect.y1)
    return rect.expanded(0.01)


def _north_arrow_regions(sheet: SheetView) -> list[ProtectedRegion]:
    """A lone, larger-than-usual ``N`` is a north arrow, not a grid letter."""
    page = sheet.page_text
    if page is None or not page.items:
        return []
    heights = [item.fh for item in page.items if item.fh > 0]
    if not heights:
        return []
    median = float(np.median(heights))
    for item in page.items:
        if _normalise(item.clean) != "N" or item.fh < median * 1.6:
            continue
        rect = sheet.item_rect(item).expanded(0.05)
        return [
            ProtectedRegion(
                type=ProtectedType.NORTH_ARROW,
                rect=rect.clipped(),
                label="North Arrow",
                confidence=0.7,
                evidence=["A single large 'N' here reads as a north arrow, so it is kept."],
            )
        ]
    return []
