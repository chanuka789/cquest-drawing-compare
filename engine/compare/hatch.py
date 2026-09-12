"""Task 5.10 — hatch as regions, never as segments.

A hatched wall is not one object. In a plotted PDF it is commonly two
thousand short parallel line segments, and a change from blockwork hatch to
concrete hatch replaces every one of them.

Compared segment by segment, that single material change — one of the most
valuable findings the tool can produce, because it is a different trade —
arrives as two thousand change records and destroys the report. Nobody reads
past it, and nobody trusts the tool again.

So hatch is found first, characterised as a region, compared as a region, and
reported as **one record**. Its segments are then withheld from the general
vector diff so they are never counted twice.

The detection is deliberately conservative. A cluster has to be dense,
parallel and *regularly spaced* before it counts as hatch: dense and parallel
alone also describes a stair, a louvre and a run of floor joists, and
collapsing those into one region would hide real changes.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from loguru import logger
from scipy.spatial import cKDTree
from shapely.geometry import MultiPoint, Polygon

from engine.compare.path_normalise import NormalisedPath
from engine.compare.tolerance import ResolvedTolerance, area_px_to_site_m2
from engine.compare.types import Bbox, ChangeKind, ChangeRecord, HatchChangeDetail, Stream

#: Segments longer than this on paper are drawing, not hatch.
MAX_HATCH_SEGMENT_PAPER_MM = 15.0
#: Hatch is drawn fine: 1-3 mm on paper whatever the drawing scale, because
#: that is what reads as a texture rather than as lines. A regular set of
#: parallel lines spaced further apart than this is a grid, a louvre or a run
#: of joists — all of which must keep reporting their changes individually.
MAX_HATCH_SPACING_PAPER_MM = 4.0
#: Angles are bucketed this finely; hatch is machine-drawn and very parallel.
ANGLE_BUCKET_DEG = 2.0
#: A cluster needs this many segments before it can be a hatched region.
MIN_HATCH_SEGMENTS = 20
#: Spacing this irregular is not hatch. Stairs and joists fail here.
MAX_SPACING_VARIATION = 0.45
#: Regions overlapping by more than this are the same region on two sheets.
REGION_MATCH_IOU = 0.5


@dataclass(slots=True)
class HatchConfig:
    """Detection thresholds, all tunable from the profile."""

    max_segment_paper_mm: float = MAX_HATCH_SEGMENT_PAPER_MM
    max_spacing_paper_mm: float = MAX_HATCH_SPACING_PAPER_MM
    angle_bucket_deg: float = ANGLE_BUCKET_DEG
    min_segments: int = MIN_HATCH_SEGMENTS
    max_spacing_variation: float = MAX_SPACING_VARIATION
    match_iou: float = REGION_MATCH_IOU
    #: Two clusters closer than this in angle are the same direction.
    cross_hatch_min_angle_deg: float = 20.0


@dataclass(slots=True)
class HatchRegion:
    """One hatched area, characterised well enough to compare."""

    bbox: Bbox
    #: Concave hull of the segment midpoints, in image pixels.
    polygon: list[tuple[float, float]] = field(default_factory=list)
    angles_deg: tuple[float, ...] = ()
    spacing_px: float = 0.0
    segment_count: int = 0
    cross_hatch: bool = False
    #: Indices into the normalised path list, so they can be withheld.
    path_indices: set[int] = field(default_factory=set)
    area_px: float = 0.0

    @property
    def dominant_angle(self) -> float:
        return self.angles_deg[0] if self.angles_deg else 0.0

    def signature(self, px_per_mm: float) -> str:
        """Angle, spacing and cross-hatch — what makes one pattern different.

        Spacing is quoted in tenths of a paper millimetre so the signature is
        independent of DPI; two renders of the same sheet must agree.
        """
        angles = "+".join(f"{angle:.0f}" for angle in self.angles_deg)
        spacing_mm = self.spacing_px / px_per_mm if px_per_mm else 0.0
        return f"{angles}@{spacing_mm:.1f}mm{'x' if self.cross_hatch else ''}"

    def area_m2(self, tolerance: ResolvedTolerance) -> float | None:
        return area_px_to_site_m2(self.area_px, tolerance)

    def as_dict(self, tolerance: ResolvedTolerance) -> dict[str, Any]:
        return {
            "signature": self.signature(tolerance.px_per_mm),
            "angles_deg": list(self.angles_deg),
            "spacing_paper_mm": round(self.spacing_px / tolerance.px_per_mm, 2)
            if tolerance.px_per_mm
            else 0.0,
            "segment_count": self.segment_count,
            "cross_hatch": self.cross_hatch,
            "area_m2": self.area_m2(tolerance),
        }


# ── Detection ───────────────────────────────────────────────────────────


def detect_hatch_regions(
    paths: list[NormalisedPath],
    tolerance: ResolvedTolerance,
    config: HatchConfig | None = None,
) -> list[HatchRegion]:
    """Find the hatched areas in a normalised path set."""
    config = config or HatchConfig()
    limit_px = config.max_segment_paper_mm * tolerance.px_per_mm

    candidates: list[tuple[int, NormalisedPath, float, tuple[float, float]]] = []
    for index, path in enumerate(paths):
        if path.point_count != 2 or path.length > limit_px or path.length <= 0:
            continue
        (x0, y0), (x1, y1) = path.points
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
        midpoint = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        candidates.append((index, path, angle, midpoint))

    if len(candidates) < config.min_segments:
        return []

    buckets: dict[int, list[int]] = defaultdict(list)
    for position, (_index, _path, angle, _midpoint) in enumerate(candidates):
        buckets[int(angle // config.angle_bucket_deg)].append(position)

    regions: list[HatchRegion] = []
    for bucket, positions in buckets.items():
        # Hatch at 0 and at 179.5 degrees is the same direction, so the
        # neighbouring bucket is merged in before anything is measured.
        neighbours = positions + buckets.get((bucket + 1) % _bucket_count(config), [])
        if len(neighbours) < config.min_segments:
            continue
        seen = set()
        unique = [
            position for position in neighbours if not (position in seen or seen.add(position))
        ]
        regions.extend(_regions_in_bucket(unique, candidates, limit_px, tolerance, config))

    regions = _drop_duplicates(regions)
    regions = _merge_cross_hatch(regions, tolerance, config)
    logger.debug("Hatch detection | candidates={} | regions={}", len(candidates), len(regions))
    return regions


#: Two regions sharing this much of their segments are the same region.
DUPLICATE_SHARE = 0.6


def _drop_duplicates(regions: list[HatchRegion]) -> list[HatchRegion]:
    """Remove regions that describe the same segments twice.

    Every angle bucket is measured together with its neighbour, so hatch
    whose angles straddle a bucket boundary is found once from each side.
    Left in, the same wall reports its pattern change twice — one correct
    record and one that scores as a false positive.
    """
    ordered = sorted(regions, key=lambda region: region.segment_count, reverse=True)
    kept: list[HatchRegion] = []
    for region in ordered:
        duplicate = False
        for existing in kept:
            shared = len(region.path_indices & existing.path_indices)
            smaller = min(len(region.path_indices), len(existing.path_indices))
            if smaller and shared / smaller >= DUPLICATE_SHARE:
                # Keep the fuller description of the same region.
                existing.path_indices |= region.path_indices
                existing.bbox = existing.bbox.union(region.bbox)
                duplicate = True
                break
        if not duplicate:
            kept.append(region)
    return kept


def _bucket_count(config: HatchConfig) -> int:
    return max(1, int(180.0 / config.angle_bucket_deg))


def _regions_in_bucket(
    positions: list[int],
    candidates: list[tuple[int, NormalisedPath, float, tuple[float, float]]],
    limit_px: float,
    tolerance: ResolvedTolerance,
    config: HatchConfig,
) -> list[HatchRegion]:
    midpoints = np.array([candidates[position][3] for position in positions], dtype=float)
    labels = _dbscan(midpoints, eps=max(limit_px * 1.5, 2.0), min_samples=3)

    regions: list[HatchRegion] = []
    for label in sorted(set(labels.tolist())):
        if label < 0:
            continue
        members = [positions[index] for index, value in enumerate(labels) if value == label]
        if len(members) < config.min_segments:
            continue
        region = _characterise(members, candidates, tolerance, config)
        if region is not None:
            regions.append(region)
    return regions


def _dbscan(points: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """Density clustering. -1 marks noise.

    Written out rather than pulled from scikit-learn: the application ships
    as a one-folder PyInstaller build and a whole ML stack is a large price
    for thirty lines.
    """
    count = len(points)
    labels = np.full(count, -1, dtype=int)
    if count == 0:
        return labels

    tree = cKDTree(points)
    neighbourhoods = tree.query_ball_point(points, r=eps)
    visited = np.zeros(count, dtype=bool)
    cluster = 0

    for index in range(count):
        if visited[index]:
            continue
        visited[index] = True
        neighbours = neighbourhoods[index]
        if len(neighbours) < min_samples:
            continue  # noise for now; may still join a cluster as a border point

        labels[index] = cluster
        queue = list(neighbours)
        while queue:
            other = queue.pop()
            if not visited[other]:
                visited[other] = True
                further = neighbourhoods[other]
                if len(further) >= min_samples:
                    queue.extend(further)
            if labels[other] < 0:
                labels[other] = cluster
        cluster += 1

    return labels


def _characterise(
    members: list[int],
    candidates: list[tuple[int, NormalisedPath, float, tuple[float, float]]],
    tolerance: ResolvedTolerance,
    config: HatchConfig,
) -> HatchRegion | None:
    """Measure a cluster and decide whether it is really hatch."""
    angles = np.array([candidates[position][2] for position in members], dtype=float)
    midpoints = np.array([candidates[position][3] for position in members], dtype=float)
    angle = float(_circular_mean_180(angles))

    spacing, variation = _spacing_of(midpoints, angle)
    if spacing <= 0 or variation > config.max_spacing_variation:
        # Dense and parallel but irregular: a stair, a louvre, a run of
        # joists. Reporting those as one region would hide a real change.
        return None
    if spacing > config.max_spacing_paper_mm * tolerance.px_per_mm:
        # Regular, parallel, but drawn too coarsely to be a texture. A
        # setting-out grid lives here, and its lines each deserve their own
        # change record.
        return None

    polygon = _hull(midpoints)
    area = _polygon_area(polygon)
    paths = [candidates[position][1] for position in members]
    bbox = paths[0].bbox
    for path in paths[1:]:
        bbox = bbox.union(path.bbox)

    return HatchRegion(
        bbox=bbox,
        polygon=polygon,
        angles_deg=(round(angle, 1),),
        spacing_px=spacing,
        segment_count=len(members),
        path_indices={candidates[position][0] for position in members},
        area_px=area if area > 0 else bbox.area,
    )


def _circular_mean_180(angles: np.ndarray) -> float:
    """Mean of angles that wrap at 180 degrees, not 360."""
    doubled = np.radians(angles * 2.0)
    mean = math.atan2(float(np.sin(doubled).mean()), float(np.cos(doubled).mean()))
    return math.degrees(mean) / 2.0 % 180.0


def _spacing_of(midpoints: np.ndarray, angle_deg: float) -> tuple[float, float]:
    """Median spacing across the hatch, and how much it varies.

    Midpoints are projected onto the normal of the hatch direction, so lines
    that sit on the same rule collapse to one offset and the gaps between
    consecutive offsets are the spacing.
    """
    if len(midpoints) < 3:
        return 0.0, 1.0
    angle = math.radians(angle_deg)
    normal = np.array([-math.sin(angle), math.cos(angle)])
    offsets = np.sort(midpoints @ normal)
    gaps = np.diff(offsets)
    real = gaps[gaps > 0.5]
    if real.size < 2:
        return 0.0, 1.0
    median = float(np.median(real))
    if median <= 0:
        return 0.0, 1.0
    # Median absolute deviation is robust to the one big gap at a region's
    # internal boundary, which a standard deviation is not.
    deviation = float(np.median(np.abs(real - median)))
    return median, deviation / median


def _hull(points: np.ndarray) -> list[tuple[float, float]]:
    """Concave hull where GEOS supports it, convex hull otherwise."""
    if len(points) < 3:
        return [(float(x), float(y)) for x, y in points]
    multipoint = MultiPoint([(float(x), float(y)) for x, y in points])
    try:
        from shapely import concave_hull

        hull = concave_hull(multipoint, ratio=0.4)
    except (ImportError, AttributeError, ValueError):
        hull = multipoint.convex_hull
    if hull.geom_type != "Polygon":
        hull = multipoint.convex_hull
    if hull.geom_type != "Polygon":
        return [(float(x), float(y)) for x, y in points]
    return [(float(x), float(y)) for x, y in hull.exterior.coords]


def _polygon_area(polygon: list[tuple[float, float]]) -> float:
    if len(polygon) < 3:
        return 0.0
    try:
        return float(Polygon(polygon).area)
    except (ValueError, TypeError):
        return 0.0


def _merge_cross_hatch(
    regions: list[HatchRegion], tolerance: ResolvedTolerance, config: HatchConfig
) -> list[HatchRegion]:
    """Two overlapping clusters at different angles are one cross-hatch."""
    merged: list[HatchRegion] = []
    consumed: set[int] = set()

    for index, region in enumerate(regions):
        if index in consumed:
            continue
        partners = []
        for other_index in range(index + 1, len(regions)):
            if other_index in consumed:
                continue
            other = regions[other_index]
            if region.bbox.iou(other.bbox) < 0.5:
                continue
            difference = abs(region.dominant_angle - other.dominant_angle)
            difference = min(difference, 180.0 - difference)
            if difference < config.cross_hatch_min_angle_deg:
                continue
            partners.append(other_index)

        if not partners:
            merged.append(region)
            continue

        combined = region
        angles = [region.dominant_angle]
        for other_index in partners:
            other = regions[other_index]
            consumed.add(other_index)
            angles.append(other.dominant_angle)
            combined = HatchRegion(
                bbox=combined.bbox.union(other.bbox),
                polygon=combined.polygon or other.polygon,
                angles_deg=tuple(sorted(angles)),
                spacing_px=min(combined.spacing_px, other.spacing_px),
                segment_count=combined.segment_count + other.segment_count,
                cross_hatch=True,
                path_indices=combined.path_indices | other.path_indices,
                area_px=max(combined.area_px, other.area_px),
            )
        merged.append(combined)

    return merged


def exclude_hatch_paths(
    paths: list[NormalisedPath],
    regions: list[HatchRegion],
    tolerance: ResolvedTolerance | None = None,
    config: HatchConfig | None = None,
    other_regions: list[HatchRegion] | None = None,
) -> list[NormalisedPath]:
    """Withhold hatch segments from the general vector diff.

    Without this every hatch segment is compared twice: once as part of its
    region and once on its own, and the second one is the two thousand
    records this whole module exists to prevent.

    Membership is not only the segments the clustering claimed. A hatch
    region's outermost segment is often too isolated to join the cluster and
    is dropped as noise — and then arrives in the report on its own as a
    98 mm line that appeared out of nowhere. Any short segment lying inside a
    detected region belongs to that region, claimed or not.

    ``regions`` are this side's own, so their segment indices apply here.
    ``other_regions`` are the opposite sheet's: only their *areas* are used,
    because a hatch that was removed leaves a region on one sheet alone and
    its segments must not return as individual removals on the other.
    """
    claimed: set[int] = set()
    for region in regions:
        claimed |= region.path_indices

    limit_px = float("inf")
    if tolerance is not None:
        config = config or HatchConfig()
        limit_px = config.max_segment_paper_mm * tolerance.px_per_mm
    margin = limit_px if limit_px != float("inf") else 0.0
    boxes = [region.bbox.expanded(margin * 0.5) for region in [*regions, *(other_regions or [])]]

    kept: list[NormalisedPath] = []
    for index, path in enumerate(paths):
        if index in claimed:
            continue
        if path.point_count == 2 and path.length <= limit_px:
            centre = path.centre_px
            if any(box.contains_point(*centre) for box in boxes):
                continue
        kept.append(path)
    return kept


# ── Comparison ──────────────────────────────────────────────────────────


def diff_hatch(
    old_regions: list[HatchRegion],
    new_regions: list[HatchRegion],
    tolerance: ResolvedTolerance,
    config: HatchConfig | None = None,
) -> list[ChangeRecord]:
    """One change record per region. Never one per segment."""
    config = config or HatchConfig()
    changes: list[ChangeRecord] = []
    matched_new: set[int] = set()

    for old in old_regions:
        best_index: int | None = None
        best_iou = config.match_iou
        for index, new in enumerate(new_regions):
            if index in matched_new:
                continue
            overlap = old.bbox.iou(new.bbox)
            if overlap >= best_iou:
                best_iou = overlap
                best_index = index

        if best_index is None:
            changes.append(_region_record(old, ChangeKind.HATCH_REMOVED, tolerance))
            continue

        new = new_regions[best_index]
        matched_new.add(best_index)
        old_signature = old.signature(tolerance.px_per_mm)
        new_signature = new.signature(tolerance.px_per_mm)

        if old_signature != new_signature:
            changes.append(_pattern_record(old, new, tolerance))
        elif best_iou < 0.95:
            changes.append(_area_record(old, new, tolerance))

    for index, new in enumerate(new_regions):
        if index not in matched_new:
            changes.append(_region_record(new, ChangeKind.HATCH_ADDED, tolerance))

    return changes


def _area_phrase(region: HatchRegion, tolerance: ResolvedTolerance) -> str:
    area = region.area_m2(tolerance)
    if area is None:
        return "area unknown without a drawing scale"
    return f"{area:.1f} m²"


def _detail(
    old: HatchRegion | None, new: HatchRegion | None, tolerance: ResolvedTolerance
) -> HatchChangeDetail:
    old_area = old.area_m2(tolerance) if old else None
    new_area = new.area_m2(tolerance) if new else None
    delta = None
    if old_area is not None and new_area is not None:
        delta = new_area - old_area
    return HatchChangeDetail(
        old_signature=old.signature(tolerance.px_per_mm) if old else None,
        new_signature=new.signature(tolerance.px_per_mm) if new else None,
        old_angle_deg=old.dominant_angle if old else None,
        new_angle_deg=new.dominant_angle if new else None,
        old_spacing_paper_mm=old.spacing_px / tolerance.px_per_mm if old else None,
        new_spacing_paper_mm=new.spacing_px / tolerance.px_per_mm if new else None,
        old_area_m2=old_area,
        new_area_m2=new_area,
        area_delta_m2=delta,
        segment_count=(new or old).segment_count if (new or old) else 0,
    )


def _region_record(
    region: HatchRegion, kind: ChangeKind, tolerance: ResolvedTolerance
) -> ChangeRecord:
    added = kind is ChangeKind.HATCH_ADDED
    detail = _detail(None if added else region, region if added else None, tolerance)
    verb = "added" if added else "removed"
    return ChangeRecord(
        kind=kind,
        bbox=region.bbox,
        streams=[Stream.HATCH],
        description=(
            f"Hatched area {verb} ({_area_phrase(region, tolerance)}, "
            f"{region.segment_count} segments as one region)"
        ),
        confidence=0.8,
        hatch=detail,
    )


def _pattern_record(
    old: HatchRegion, new: HatchRegion, tolerance: ResolvedTolerance
) -> ChangeRecord:
    """A material change: blockwork to concrete, and that is a different trade."""
    detail = _detail(old, new, tolerance)
    angle = (
        f"{old.dominant_angle:.0f}° → {new.dominant_angle:.0f}°"
        if abs(old.dominant_angle - new.dominant_angle) > 1.0
        else "same angle"
    )
    spacing_old = old.spacing_px / tolerance.px_per_mm
    spacing_new = new.spacing_px / tolerance.px_per_mm
    spacing = (
        f"spacing {spacing_old:.1f} → {spacing_new:.1f} mm on paper"
        if abs(spacing_old - spacing_new) > 0.05
        else "same spacing"
    )
    return ChangeRecord(
        kind=ChangeKind.HATCH_PATTERN_CHANGED,
        bbox=old.bbox.union(new.bbox),
        streams=[Stream.HATCH],
        description=(
            f"Hatch pattern changed over {_area_phrase(new, tolerance)} "
            f"({angle}, {spacing}). This usually means a different material."
        ),
        confidence=0.85,
        hatch=detail,
    )


def _area_record(old: HatchRegion, new: HatchRegion, tolerance: ResolvedTolerance) -> ChangeRecord:
    detail = _detail(old, new, tolerance)
    delta = detail.area_delta_m2
    phrase = f"{delta:+.1f} m²" if delta is not None else "area changed"
    return ChangeRecord(
        kind=ChangeKind.HATCH_AREA_CHANGED,
        bbox=old.bbox.union(new.bbox),
        streams=[Stream.HATCH],
        description=f"Hatched area resized ({phrase}), same pattern",
        confidence=0.8,
        hatch=detail,
    )


def region_bbox(regions: list[HatchRegion]) -> Bbox:
    if not regions:
        return Bbox(0.0, 0.0, 0.0, 0.0)
    box = regions[0].bbox
    for region in regions[1:]:
        box = box.union(region.bbox)
    return box
