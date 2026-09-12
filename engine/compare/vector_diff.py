"""Task 5.9 — comparing geometry instead of pixels.

Once both sheets' paths are normalised, most of the work is a set operation
on hashes: present in both is unchanged, old only is a candidate removal, new
only a candidate addition. That is fast and it is exact.

Two refinements turn a correct set difference into a readable report:

* **Near-matches become moves.** A wall that shifted 40 mm is one change
  ("moved"), not two ("removed here, added there"). Unmatched paths are
  searched against each other spatially and paired when their shape and
  length agree.
* **Style-only differences are cosmetic.** Identical geometry with a
  different pen, dash pattern or colour is a replot, not a design change. It
  is reported, because the user asked what changed — but marked cosmetic and
  hidden by default.

The spatial search uses an R-tree (``shapely.STRtree``). A linear scan over
fifty thousand unmatched paths against fifty thousand others is four hundred
million comparisons less than a second of index building would have cost.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from shapely import STRtree
from shapely.geometry import LineString, Point

from engine.compare.path_normalise import NormalisedPath
from engine.compare.tolerance import ResolvedTolerance
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    Stream,
    VectorChangeDetail,
)
from engine.masking.mask_engine import MaskSet

#: Two paths are the same shape when their lengths agree this closely.
LENGTH_TOLERANCE = 0.08
#: How far a path may have moved and still be reported as moved rather than
#: as a removal plus an addition. Expressed on paper, not as a multiple of
#: the position tolerance: at 1:100 that tolerance is a quarter of a
#: millimetre, and twenty times nothing is still nothing — every genuine move
#: came back as a removal plus an addition somewhere else.
MOVE_SEARCH_PAPER_MM = 50.0


@dataclass(slots=True)
class VectorDiffConfig:
    """Knobs for the vector stream."""

    length_tolerance: float = LENGTH_TOLERANCE
    move_search_paper_mm: float = MOVE_SEARCH_PAPER_MM
    #: Paths shorter than this on paper are left to the hatch detector.
    min_reportable_paper_mm: float = 0.5
    report_style_only: bool = True


@dataclass(slots=True)
class VectorDiffResult:
    """What the vector stream found."""

    changes: list[ChangeRecord] = field(default_factory=list)
    old_count: int = 0
    new_count: int = 0
    unchanged_count: int = 0
    style_only_count: int = 0
    moved_count: int = 0
    masked_old: int = 0
    masked_new: int = 0
    skip_reason: str = ""

    @property
    def ran(self) -> bool:
        return not self.skip_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_count": self.old_count,
            "new_count": self.new_count,
            "unchanged_count": self.unchanged_count,
            "style_only_count": self.style_only_count,
            "moved_count": self.moved_count,
            "changes": len(self.changes),
            "skip_reason": self.skip_reason,
        }


def _mask_paths(
    paths: list[NormalisedPath], mask: MaskSet | None, width_px: int, height_px: int
) -> tuple[list[NormalisedPath], int]:
    if mask is None or width_px <= 0 or height_px <= 0:
        return paths, 0
    kept: list[NormalisedPath] = []
    dropped = 0
    for path in paths:
        cx, cy = path.centre_px
        if mask.covers(cx / width_px, cy / height_px):
            dropped += 1
            continue
        kept.append(path)
    return kept, dropped


def diff_vectors(
    old_paths: list[NormalisedPath],
    new_paths: list[NormalisedPath],
    tolerance: ResolvedTolerance,
    *,
    mask: MaskSet | None = None,
    page_px: tuple[int, int] = (0, 0),
    config: VectorDiffConfig | None = None,
    skip_reason: str = "",
) -> VectorDiffResult:
    """Compare two normalised path sets.

    Both sets must already be in new-sheet pixel space — the caller applies
    the Phase 4 transform during normalisation.
    """
    config = config or VectorDiffConfig()
    result = VectorDiffResult(old_count=len(old_paths), new_count=len(new_paths))
    if skip_reason:
        result.skip_reason = skip_reason
        return result

    width_px, height_px = page_px
    old_paths, result.masked_old = _mask_paths(old_paths, mask, width_px, height_px)
    new_paths, result.masked_new = _mask_paths(new_paths, mask, width_px, height_px)

    old_by_hash: dict[str, list[NormalisedPath]] = defaultdict(list)
    new_by_hash: dict[str, list[NormalisedPath]] = defaultdict(list)
    for path in old_paths:
        old_by_hash[path.geometry_hash].append(path)
    for path in new_paths:
        new_by_hash[path.geometry_hash].append(path)

    removed: list[NormalisedPath] = []
    added: list[NormalisedPath] = []

    for hash_value in set(old_by_hash) | set(new_by_hash):
        olds = old_by_hash.get(hash_value, [])
        news = new_by_hash.get(hash_value, [])
        shared = min(len(olds), len(news))
        result.unchanged_count += shared

        # Same geometry on both sides: only the graphics state can differ.
        for index in range(shared):
            if olds[index].style_hash == news[index].style_hash:
                continue
            result.style_only_count += 1
            if config.report_style_only:
                result.changes.append(_style_record(olds[index], news[index], tolerance))

        removed.extend(olds[shared:])
        added.extend(news[shared:])

    moved_pairs, removed, added = _find_moves(removed, added, tolerance, config)
    result.moved_count = len(moved_pairs)
    for old, new in moved_pairs:
        result.changes.append(_moved_record(old, new, tolerance))

    minimum_px = config.min_reportable_paper_mm * tolerance.px_per_mm
    for path in removed:
        if path.length < minimum_px:
            continue
        result.changes.append(_simple_record(path, ChangeKind.REMOVED, tolerance))
    for path in added:
        if path.length < minimum_px:
            continue
        result.changes.append(_simple_record(path, ChangeKind.ADDED, tolerance))

    logger.debug(
        "Vector diff | old={} new={} unchanged={} moved={} style={} changes={}",
        result.old_count,
        result.new_count,
        result.unchanged_count,
        result.moved_count,
        result.style_only_count,
        len(result.changes),
    )
    return result


def _find_moves(
    removed: list[NormalisedPath],
    added: list[NormalisedPath],
    tolerance: ResolvedTolerance,
    config: VectorDiffConfig,
) -> tuple[list[tuple[NormalisedPath, NormalisedPath]], list[NormalisedPath], list[NormalisedPath]]:
    """Pair unmatched old and new paths that are the same shape nearby."""
    if not removed or not added:
        return [], removed, added

    radius = max(config.move_search_paper_mm * tolerance.px_per_mm, tolerance.position.px * 4.0)
    geometries = [_geometry_of(path) for path in added]
    tree = STRtree(geometries)

    pairs: list[tuple[NormalisedPath, NormalisedPath]] = []
    used: set[int] = set()
    unmatched_old: list[NormalisedPath] = []

    for old in removed:
        query = _geometry_of(old).buffer(radius)
        best_index: int | None = None
        best_distance = float("inf")
        for index in tree.query(query):
            index = int(index)
            if index in used:
                continue
            candidate = added[index]
            if not _same_shape(old, candidate, config.length_tolerance):
                continue
            distance = math.hypot(candidate.bbox.cx - old.bbox.cx, candidate.bbox.cy - old.bbox.cy)
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index is None:
            unmatched_old.append(old)
            continue
        used.add(best_index)
        pairs.append((old, added[best_index]))

    unmatched_new = [path for index, path in enumerate(added) if index not in used]
    return pairs, unmatched_old, unmatched_new


def _geometry_of(path: NormalisedPath) -> Any:
    if len(path.points) >= 2:
        return LineString(path.points)
    return Point(path.points[0]) if path.points else Point(0, 0)


def _same_shape(first: NormalisedPath, second: NormalisedPath, length_tolerance: float) -> bool:
    """Same kind of thing, the same size — allowing it to sit elsewhere."""
    if first.geometry_type != second.geometry_type:
        return False
    if first.closed != second.closed:
        return False
    longer = max(first.length, second.length)
    if longer <= 0:
        return False
    if abs(first.length - second.length) / longer > length_tolerance:
        return False
    # Same shape means the same box proportions, wherever the box sits.
    return _similar(first.bbox.w, second.bbox.w) and _similar(first.bbox.h, second.bbox.h)


def _similar(first: float, second: float, tolerance: float = 0.1) -> bool:
    longer = max(abs(first), abs(second))
    if longer < 1e-6:
        return True
    return abs(first - second) / longer <= tolerance


# ── Records ─────────────────────────────────────────────────────────────


def _detail(
    path: NormalisedPath, tolerance: ResolvedTolerance, count: int = 1
) -> VectorChangeDetail:
    paper_mm = tolerance.px_to_paper_mm(path.length)
    return VectorChangeDetail(
        path_count=count,
        total_length_paper_mm=paper_mm,
        total_length_site_mm=tolerance.px_to_site_mm(path.length),
        geometry_type=path.geometry_type,
    )


def _length_phrase(path: NormalisedPath, tolerance: ResolvedTolerance) -> str:
    site = tolerance.px_to_site_mm(path.length)
    if site is None:
        return f"{tolerance.px_to_paper_mm(path.length):.1f} mm on paper"
    if site >= 1000:
        return f"{site / 1000:.2f} m on site"
    return f"{site:.0f} mm on site"


def _simple_record(
    path: NormalisedPath, kind: ChangeKind, tolerance: ResolvedTolerance
) -> ChangeRecord:
    verb = "added" if kind is ChangeKind.ADDED else "removed"
    return ChangeRecord(
        kind=kind,
        bbox=path.bbox,
        streams=[Stream.VECTOR],
        description=f"{path.geometry_type.title()} {verb} ({_length_phrase(path, tolerance)})",
        confidence=0.8,
        vector=_detail(path, tolerance),
    )


def _moved_record(
    old: NormalisedPath, new: NormalisedPath, tolerance: ResolvedTolerance
) -> ChangeRecord:
    distance_px = math.hypot(new.bbox.cx - old.bbox.cx, new.bbox.cy - old.bbox.cy)
    site = tolerance.px_to_site_mm(distance_px)
    moved = (
        f"{site:.0f} mm on site"
        if site is not None
        else f"{tolerance.px_to_paper_mm(distance_px):.1f} mm on paper"
    )
    return ChangeRecord(
        kind=ChangeKind.MOVED,
        bbox=old.bbox.union(new.bbox),
        streams=[Stream.VECTOR],
        description=f"{new.geometry_type.title()} moved {moved}",
        confidence=0.75,
        vector=_detail(new, tolerance),
        detail={"distance_px": distance_px},
    )


def _style_record(
    old: NormalisedPath, new: NormalisedPath, tolerance: ResolvedTolerance
) -> ChangeRecord:
    differences: list[str] = []
    if abs(old.line_width_px - new.line_width_px) > 0.01:
        differences.append(
            f"line weight {tolerance.px_to_paper_mm(old.line_width_px):.2f} mm → "
            f"{tolerance.px_to_paper_mm(new.line_width_px):.2f} mm"
        )
    if old.dash_array != new.dash_array:
        differences.append("line type" if new.dash_array else "solid instead of dashed")
    if old.stroke_colour != new.stroke_colour:
        differences.append("colour")
    description = ", ".join(differences) or "graphics state"

    detail = _detail(new, tolerance)
    detail.style_difference = description
    return ChangeRecord(
        kind=ChangeKind.STYLE_ONLY,
        bbox=new.bbox,
        streams=[Stream.VECTOR],
        description=f"Same geometry, different {description}",
        confidence=0.9,
        is_cosmetic=True,
        vector=detail,
    )


def bbox_of(paths: list[NormalisedPath]) -> Bbox:
    """One box round a set of paths."""
    if not paths:
        return Bbox(0.0, 0.0, 0.0, 0.0)
    box = paths[0].bbox
    for path in paths[1:]:
        box = box.union(path.bbox)
    return box
