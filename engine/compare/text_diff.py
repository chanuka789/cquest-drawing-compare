"""Task 5.6 — text matching and diff. The highest-value output of Phase 5.

Four of the five changes that actually cost money on a construction project
are text changes: a dimension, a note, a wall tag, a level. In a pixel diff
they are a handful of dark pixels lost among thousands from line weight
noise. Here they are the first thing on the report, with the old value, the
new value and the delta.

**Matching uses optimal assignment, not a greedy nearest-match loop.** This
is not a refinement. In a dimension string five numbers sit within
centimetres of each other; a greedy loop pairs `3000` with the wrong `3000`,
and one real change becomes two false ones — a removal and an addition in
places where nothing happened. The same reasoning as Phase 3 matching, for
the same reason.

The matching order is exact, then moved, then modified, then the leftovers.
Each stage removes what it consumed, so the expensive fuzzy stage only ever
sees text that nothing simpler could explain.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from loguru import logger
from numpy.typing import NDArray
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from engine.align.anchors import page_points_to_image_px
from engine.compare.dimension_diff import analyse_numeric
from engine.compare.text_classify import (
    ClassifyConfig,
    ClassifyContext,
    ParsedNumber,
    classify,
    parse_numeric,
)
from engine.compare.tolerance import ResolvedTolerance
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    Stream,
    TextCategory,
    TextChangeDetail,
)
from engine.extract.text_extractor import TextItem
from engine.masking.mask_engine import MaskSet
from engine.masking.types import SheetView


@dataclass(slots=True)
class TextDiffConfig:
    """Everything the text stream can be tuned by."""

    #: How far apart two copies of the same string may sit and still be the
    #: same item, on paper. Falls back to the resolved position tolerance.
    search_radius_paper_mm: float = 15.0
    #: Below this combined score a candidate pair is not a modification.
    modify_score_threshold: float = 0.55
    #: How much of the score is string similarity; the rest is proximity.
    similarity_weight: float = 0.7
    #: Case changes in specification text can matter, so folding is opt-in.
    case_fold: bool = True
    #: A renumbering needs at least this many consistent tag changes.
    min_renumber_count: int = 4
    #: Identical strings whose boxes still overlap this much have not moved,
    #: however far apart their centres are.
    #:
    #: A text run's box is set by its glyph metrics, so a substituted font —
    #: the office's font missing on the plotting machine — shifts every
    #: centre on the sheet by more than the position tolerance while nothing
    #: has actually moved. Overlap is the physical test: a label that shifted
    #: less than half its own width is where it was.
    move_min_separation_iou: float = 0.5
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)


# ── Items placed in comparison space ────────────────────────────────────


@dataclass(slots=True)
class TextItemView:
    """One text item, in new-sheet pixel space, classified and normalised."""

    text: str
    normalised: str
    bbox: Bbox
    category: TextCategory = TextCategory.UNKNOWN
    category_confidence: float = 0.0
    number: ParsedNumber | None = None
    #: Which sheet it came from, for logs.
    side: str = "new"
    source_index: int = -1

    @property
    def centre(self) -> tuple[float, float]:
        return self.bbox.centre


def normalise_text(text: str, *, case_fold: bool = True) -> str:
    """NFKC, collapsed whitespace, optionally case folded.

    Case folding is on by default because drawing offices are inconsistent
    about it, and a sheet replotted with a different text style should not
    report every label as modified. It is configurable because a change from
    `min` to `MIN` inside a specification note can matter.
    """
    cleaned = " ".join(unicodedata.normalize("NFKC", text).split())
    return cleaned.upper() if case_fold else cleaned


def prepare_items(
    sheet: SheetView,
    *,
    mask: MaskSet | None = None,
    transform: NDArray[np.float64] | None = None,
    context: ClassifyContext | None = None,
    config: TextDiffConfig | None = None,
    side: str = "new",
) -> tuple[list[TextItemView], list[TextItem]]:
    """Place one sheet's text in comparison space, classified and masked.

    Returns (kept, dropped). The dropped list is what the mask excluded —
    kept so the debug view can show it rather than it vanishing.
    """
    config = config or TextDiffConfig()
    page = sheet.page_text
    if page is None or page.is_empty:
        return [], []

    kept: list[TextItemView] = []
    dropped: list[TextItem] = []
    for index, item in enumerate(page.items):
        text = item.clean
        normalised = normalise_text(text, case_fold=True)
        if mask is not None:
            fx, fy = sheet.item_centre(item)
            # covers_text, not covers: a watermark's box spans a third of the
            # sheet, and only the watermark's own string belongs to it.
            if mask.covers_text(fx, fy, normalised):
                dropped.append(item)
                continue
            # A big watermark's glyphs land inside the boxes of the labels
            # under it, so the reader hands back `RM-02 P`. Strip them here,
            # once, rather than reporting a tag that never changed.
            text = mask.clean_text(fx, fy, text)

        bbox = _item_bbox_px(item, sheet)
        if transform is not None:
            bbox = warp_bbox(bbox, transform)

        item_context = context or ClassifyContext(px_per_mm=sheet.px_per_mm)
        result = classify(text, bbox, item_context, config.classify)
        kept.append(
            TextItemView(
                text=text,
                normalised=normalise_text(text, case_fold=config.case_fold),
                bbox=bbox,
                category=result.category,
                category_confidence=result.confidence,
                number=result.number or parse_numeric(text),
                side=side,
                source_index=index,
            )
        )
    return kept, dropped


def _item_bbox_px(item: TextItem, sheet: SheetView) -> Bbox:
    """One text item's box in image pixels, y down.

    The page box origin and the y flip are handled by the Phase 4 helper, so
    a sheet whose media box starts at (-1192, -842) lands on the same pixels
    as one that starts at the origin.
    """
    page = sheet.page_text
    if page is None:
        return Bbox(0.0, 0.0, 0.0, 0.0)
    box = page.box
    left, top = page_points_to_image_px(
        item.x, item.top, box.x0, box.y0, box.width, box.height, sheet.dpi
    )
    right, bottom = page_points_to_image_px(
        item.right, item.y, box.x0, box.y0, box.width, box.height, sheet.dpi
    )
    return Bbox.from_corners(left, top, right, bottom)


def warp_bbox(bbox: Bbox, transform: NDArray[np.float64]) -> Bbox:
    """Map a box through the Phase 4 alignment matrix (old px -> new px)."""
    corners = np.array(
        [
            [bbox.x, bbox.y],
            [bbox.right, bbox.y],
            [bbox.x, bbox.bottom],
            [bbox.right, bbox.bottom],
        ],
        dtype=float,
    )
    homogeneous = np.column_stack([corners, np.ones(len(corners))])
    warped = homogeneous @ np.asarray(transform, dtype=float).T
    points = warped[:, :2] / warped[:, 2:3]
    return Bbox.from_points([(float(x), float(y)) for x, y in points])


# ── Results ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RenumberPattern:
    """Many tags renumbered by one consistent rule. Reported once, not N times."""

    mapping: dict[str, str] = field(default_factory=dict)
    rule: str = ""
    bbox: Bbox = field(default_factory=lambda: Bbox(0.0, 0.0, 0.0, 0.0))
    count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"mapping": dict(self.mapping), "rule": self.rule, "count": self.count}


@dataclass(slots=True)
class TextDiffResult:
    """Everything the text stream found, plus what it needs to tell others."""

    changes: list[ChangeRecord] = field(default_factory=list)
    #: Boxes of text the stream is confident did not change. The font
    #: substitution filter uses these to suppress raster noise inside them.
    unchanged_boxes: list[Bbox] = field(default_factory=list)
    #: Unchanged dimensions, for the geometry-moved-dimension-stale check.
    unchanged_dimensions: list[tuple[Bbox, str]] = field(default_factory=list)
    renumbering: RenumberPattern | None = None
    old_count: int = 0
    new_count: int = 0
    unchanged_count: int = 0
    masked_old: int = 0
    masked_new: int = 0

    @property
    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for change in self.changes:
            counts[str(change.kind)] += 1
        return dict(counts)


# ── The diff ────────────────────────────────────────────────────────────


def diff_text(
    old_items: list[TextItemView],
    new_items: list[TextItemView],
    tolerance: ResolvedTolerance,
    *,
    config: TextDiffConfig | None = None,
) -> TextDiffResult:
    """Match two sets of placed text and describe what differs.

    Both sets must already be in the same coordinate space — the caller warps
    the old side with the Phase 4 transform in :func:`prepare_items`.
    """
    config = config or TextDiffConfig()
    result = TextDiffResult(old_count=len(old_items), new_count=len(new_items))

    position_tolerance_px = max(tolerance.position.px, 1.0)
    old_left, new_left = _match_identical(
        old_items, new_items, position_tolerance_px, result, config
    )
    _match_modified(old_left, new_left, tolerance, config, result)

    result.renumbering = detect_renumbering(result.changes, config)
    if result.renumbering is not None:
        _collapse_renumbering(result)

    logger.debug(
        "Text diff | old={} new={} unchanged={} changes={}",
        result.old_count,
        result.new_count,
        result.unchanged_count,
        len(result.changes),
    )
    return result


def _match_identical(
    old_items: list[TextItemView],
    new_items: list[TextItemView],
    tolerance_px: float,
    result: TextDiffResult,
    config: TextDiffConfig,
) -> tuple[list[TextItemView], list[TextItemView]]:
    """Pair up identical strings, then split them into unchanged and moved.

    Identical strings are paired by *optimal assignment on distance*, not by
    order of appearance. A sheet with twelve doors labelled `D-12` must pair
    each old copy with the nearest free new copy, or eleven of them report as
    moved across the sheet.
    """
    old_by_text: dict[str, list[TextItemView]] = defaultdict(list)
    new_by_text: dict[str, list[TextItemView]] = defaultdict(list)
    for item in old_items:
        old_by_text[item.normalised].append(item)
    for item in new_items:
        new_by_text[item.normalised].append(item)

    old_left: list[TextItemView] = []
    new_left: list[TextItemView] = []

    for text in set(old_by_text) | set(new_by_text):
        olds = old_by_text.get(text, [])
        news = new_by_text.get(text, [])
        if not olds or not news:
            old_left.extend(olds)
            new_left.extend(news)
            continue

        cost = np.array(
            [[_distance(old.centre, new.centre) for new in news] for old in olds], dtype=float
        )
        rows, columns = linear_sum_assignment(cost)
        paired_old = set(rows.tolist())
        paired_new = set(columns.tolist())

        for row, column in zip(rows, columns, strict=True):
            old, new = olds[row], news[column]
            distance = float(cost[row, column])
            still_overlapping = old.bbox.iou(new.bbox) >= config.move_min_separation_iou
            if distance <= tolerance_px or still_overlapping:
                result.unchanged_count += 1
                # The union of both boxes, not just the new one. A
                # substituted font sets the same string to a different width,
                # so the old ink reaches past the new box; suppressing raster
                # noise inside the new box alone leaves the overhang behind
                # as a change.
                result.unchanged_boxes.append(old.bbox.union(new.bbox))
                if new.category in {TextCategory.DIMENSION, TextCategory.LEVEL}:
                    result.unchanged_dimensions.append((new.bbox, new.text))
                continue
            result.changes.append(_moved_record(old, new, distance))

        old_left.extend(olds[index] for index in range(len(olds)) if index not in paired_old)
        new_left.extend(news[index] for index in range(len(news)) if index not in paired_new)

    return old_left, new_left


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _match_modified(
    old_left: list[TextItemView],
    new_left: list[TextItemView],
    tolerance: ResolvedTolerance,
    config: TextDiffConfig,
    result: TextDiffResult,
) -> None:
    """Pair what is left by string similarity and proximity, optimally.

    Only pairs inside the search radius are candidates; everything the
    assignment does not pair, or pairs below the score threshold, becomes an
    addition or a removal.
    """
    if not old_left or not new_left:
        for item in old_left:
            result.changes.append(_removed_record(item))
        for item in new_left:
            result.changes.append(_added_record(item))
        return

    radius_px = max(
        tolerance.paper_mm_to_px(config.search_radius_paper_mm), tolerance.position.px * 2.0
    )
    scores = np.zeros((len(old_left), len(new_left)), dtype=float)
    for row, old in enumerate(old_left):
        for column, new in enumerate(new_left):
            scores[row, column] = _pair_score(old, new, radius_px, config)

    # linear_sum_assignment minimises, so cost is the complement of the score.
    rows, columns = linear_sum_assignment(1.0 - scores)
    paired_old: set[int] = set()
    paired_new: set[int] = set()

    for row, column in zip(rows, columns, strict=True):
        score = float(scores[row, column])
        if score < config.modify_score_threshold:
            continue
        old, new = old_left[row], new_left[column]
        paired_old.add(row)
        paired_new.add(column)
        result.changes.append(_modified_record(old, new, score))

    for index, item in enumerate(old_left):
        if index not in paired_old:
            result.changes.append(_removed_record(item))
    for index, item in enumerate(new_left):
        if index not in paired_new:
            result.changes.append(_added_record(item))


def _pair_score(
    old: TextItemView, new: TextItemView, radius_px: float, config: TextDiffConfig
) -> float:
    """How likely one old item is a modified version of one new item."""
    distance = _distance(old.centre, new.centre)
    if distance > radius_px:
        return 0.0
    similarity = fuzz.ratio(old.normalised, new.normalised) / 100.0
    proximity = 1.0 - (distance / radius_px)
    score = config.similarity_weight * similarity + (1.0 - config.similarity_weight) * proximity
    # Two numbers in the same place are a far better candidate pair than a
    # number and a word, whatever the character overlap says.
    if old.number is not None and new.number is not None:
        score = min(1.0, score + 0.1)
    if old.category is new.category:
        score = min(1.0, score + 0.05)
    return score


# ── Records ─────────────────────────────────────────────────────────────


def _detail(
    old: TextItemView | None, new: TextItemView | None, similarity: float = 0.0
) -> TextChangeDetail:
    reference = new or old
    assert reference is not None
    return TextChangeDetail(
        category=reference.category,
        old_text=old.text if old else None,
        new_text=new.text if new else None,
        old_position=old.centre if old else None,
        new_position=new.centre if new else None,
        similarity=similarity,
    )


def _record(
    kind: ChangeKind,
    bbox: Bbox,
    detail: TextChangeDetail,
    description: str,
    confidence: float,
) -> ChangeRecord:
    return ChangeRecord(
        kind=kind,
        bbox=bbox,
        streams=[Stream.TEXT],
        description=description,
        confidence=confidence,
        text=detail,
    )


def _category_word(category: TextCategory) -> str:
    words = {
        TextCategory.DIMENSION: "Dimension",
        TextCategory.LEVEL: "Level",
        TextCategory.TAG: "Tag",
        TextCategory.ROOM: "Room label",
        TextCategory.SCALE: "Scale",
        TextCategory.GRID: "Grid reference",
        TextCategory.NOTE: "Note",
        TextCategory.SPEC: "Specification text",
        TextCategory.TITLEBLOCK: "Title block text",
        TextCategory.UNKNOWN: "Text",
    }
    return words[category]


def _moved_record(old: TextItemView, new: TextItemView, distance_px: float) -> ChangeRecord:
    detail = _detail(old, new, similarity=1.0)
    return _record(
        ChangeKind.MOVED,
        old.bbox.union(new.bbox),
        detail,
        f"{_category_word(new.category)} moved: {new.text}",
        0.8,
    )


def _modified_record(old: TextItemView, new: TextItemView, score: float) -> ChangeRecord:
    detail = _detail(old, new, similarity=score)
    analysis = analyse_numeric(
        old.text,
        new.text,
        category=new.category,
        old_number=old.number,
        new_number=new.number,
    )
    detail.numeric_delta = analysis.delta
    detail.percent_delta = analysis.percent_delta
    detail.site_delta_mm = analysis.site_delta_mm
    description = f"{_category_word(new.category)} changed: {analysis.description}"
    return _record(ChangeKind.MODIFIED, old.bbox.union(new.bbox), detail, description, score)


def _added_record(new: TextItemView) -> ChangeRecord:
    return _record(
        ChangeKind.ADDED,
        new.bbox,
        _detail(None, new),
        f"{_category_word(new.category)} added: {new.text}",
        0.75,
    )


def _removed_record(old: TextItemView) -> ChangeRecord:
    return _record(
        ChangeKind.REMOVED,
        old.bbox,
        _detail(old, None),
        f"{_category_word(old.category)} removed: {old.text}",
        0.75,
    )


# ── Renumbering ─────────────────────────────────────────────────────────

_TAG_PARTS = re.compile(r"^(?P<prefix>[A-Za-z]*)[-/ ]?(?P<number>\d+)(?P<suffix>[A-Za-z]*)$")


def detect_renumbering(
    changes: list[ChangeRecord], config: TextDiffConfig | None = None
) -> RenumberPattern | None:
    """One record for a bulk renumbering, instead of dozens of tag changes.

    The signature is: many `tag` modifications, each staying where it was,
    whose old and new values follow *one* rule — the same prefix change, or
    the same numeric offset. Anything less consistent than that is left as
    individual changes, because a handful of unrelated tag edits is exactly
    what a user wants to see one by one.
    """
    config = config or TextDiffConfig()
    candidates = [
        change
        for change in changes
        if change.kind is ChangeKind.MODIFIED
        and change.text is not None
        and change.text.category is TextCategory.TAG
        and change.text.old_text
        and change.text.new_text
    ]
    if len(candidates) < config.min_renumber_count:
        return None

    mapping: dict[str, str] = {}
    offsets: set[int] = set()
    prefix_changes: set[tuple[str, str]] = set()
    for change in candidates:
        detail = change.text
        assert detail is not None and detail.old_text and detail.new_text
        mapping[detail.old_text] = detail.new_text
        old_parts = _TAG_PARTS.match(detail.old_text.replace(" ", ""))
        new_parts = _TAG_PARTS.match(detail.new_text.replace(" ", ""))
        if not old_parts or not new_parts:
            return None
        offsets.add(int(new_parts.group("number")) - int(old_parts.group("number")))
        prefix_changes.add((old_parts.group("prefix"), new_parts.group("prefix")))

    if len(mapping) != len(set(mapping.values())):
        return None  # not a bijection: two tags became the same tag

    rule = ""
    if len(offsets) == 1 and len(prefix_changes) == 1:
        offset = next(iter(offsets))
        old_prefix, new_prefix = next(iter(prefix_changes))
        if old_prefix != new_prefix and offset == 0:
            rule = f"every '{old_prefix}' tag became '{new_prefix}'"
        elif old_prefix == new_prefix and offset != 0:
            rule = f"every {old_prefix} tag number shifted by {offset:+d}"
        elif old_prefix != new_prefix:
            rule = f"'{old_prefix}' became '{new_prefix}' and numbers shifted by {offset:+d}"
    if not rule:
        return None

    boxes = [change.bbox for change in candidates]
    bbox = boxes[0]
    for box in boxes[1:]:
        bbox = bbox.union(box)

    return RenumberPattern(mapping=mapping, rule=rule, bbox=bbox, count=len(candidates))


def _collapse_renumbering(result: TextDiffResult) -> None:
    """Replace the individual tag changes with the single renumbering record."""
    pattern = result.renumbering
    if pattern is None:
        return
    remaining = [
        change
        for change in result.changes
        if not (
            change.kind is ChangeKind.MODIFIED
            and change.text is not None
            and change.text.category is TextCategory.TAG
            and change.text.old_text in pattern.mapping
        )
    ]
    sample = ", ".join(f"{old} → {new}" for old, new in list(pattern.mapping.items())[:3])
    record = ChangeRecord(
        kind=ChangeKind.TAGS_RENUMBERED,
        bbox=pattern.bbox,
        streams=[Stream.TEXT],
        description=(f"{pattern.count} tags renumbered — {pattern.rule}. For example {sample}."),
        confidence=0.9,
        text=TextChangeDetail(category=TextCategory.TAG),
        detail={"mapping": dict(pattern.mapping), "rule": pattern.rule},
    )
    remaining.append(record)
    result.changes = remaining
