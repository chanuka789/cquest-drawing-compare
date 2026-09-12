"""Task 5.14 — the comparison orchestrator.

Four streams look at the same two sheets and find overlapping things. This is
where they become one answer.

The order of the pipeline is not arbitrary:

1. **Refuse a bad alignment.** A comparison of two sheets that did not align
   is not a worse result, it is a wrong one, and it looks convincing. Phase 4
   already decided; this only honours the decision.
2. **Text always runs**, because it is the highest-value output and it is the
   one stream a sub-millimetre alignment error cannot damage.
3. **Vector runs when there are vectors**, hatch inside it, and the raster
   stream always runs as the safety net.
4. **Residual before filters.** If the change map is tracing existing strokes
   then there is nothing worth filtering; the honest output is a warning
   about the alignment, not four hundred halo fragments.
5. **Deduplicate across streams**, because one physical change appears in
   more than one of them. Two streams agreeing is a stronger result, so the
   merged record keeps the richest description and gains confidence.
6. **Cluster geometry, never text.** Eight lines deleted inside one room are
   one change region; two dimensions that changed in the same corner are two
   findings, each with its own old and new value.

Everything the run used — the configuration, the tolerances, the mask, the
engine version — is snapshotted into the result. If this output ever supports
a variation claim it has to be reproducible.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.compare.dimension_diff import (
    cross_check_geometry,
    estimate_quantity_impact,
    find_stale_dimensions,
)
from engine.compare.hatch import HatchConfig, detect_hatch_regions, diff_hatch, exclude_hatch_paths
from engine.compare.noise_filter import FilterConfig, run_filters
from engine.compare.path_normalise import NormalisedPath, normalise_paths, transform_points
from engine.compare.raster_diff import RasterDiffConfig, diff_raster
from engine.compare.text_classify import ClassifyContext
from engine.compare.text_diff import TextDiffConfig, diff_text, prepare_items
from engine.compare.tolerance import (
    ResolvedTolerance,
    SheetToleranceOverride,
    ToleranceSpec,
    resolve_for_sheet,
)
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    CrossCheckFlag,
    FilteredChange,
    Stream,
    StreamStats,
    TextCategory,
    TextChangeDetail,
    Warning_,
)
from engine.compare.vector_diff import VectorDiffConfig, diff_vectors
from engine.extract.raster_renderer import RenderOptions, render_page
from engine.extract.text_extractor import extract_page_text
from engine.extract.vector_extractor import LayerInfo, extract_vector_page
from engine.masking.mask_engine import MaskSet, build_mask
from engine.masking.titleblock_zones import RevisionRow, detect_zones
from engine.masking.types import SheetView
from engine.masking.watermark_detect import (
    detect_annotations,
    detect_stamps,
    detect_watermarks,
    detections_to_zones,
)
from engine.utils.pdf_runtime import open_document

#: Bumped whenever a change to this package could alter a result. Stored with
#: every run so an old comparison can be told apart from a new one.
COMPARE_ENGINE_VERSION = "5.0.0"


@dataclass(slots=True)
class SheetSource:
    """One side of a pair: where the sheet is and what is known about it."""

    path: str
    page_index: int = 0
    scale_text: str | None = None
    scale_denominator: int | None = None
    label: str = ""


@dataclass(slots=True)
class PairInput:
    """The two sheets being compared."""

    old: SheetSource
    new: SheetSource
    pair_id: str = ""
    label: str = ""


@dataclass(slots=True)
class AlignmentInput:
    """The Phase 4 result, as this phase needs it."""

    #: 3x3, mapping old-sheet pixels onto new-sheet pixels at :attr:`dpi`.
    matrix: NDArray[np.float64] | None = None
    verdict: str = "good"
    dpi: int = 200
    #: True when the user chose to compare despite a poor verdict.
    user_override: bool = False
    rms_mm_on_paper: float | None = None
    rms_mm_on_site: float | None = None

    @property
    def proceeds(self) -> bool:
        return self.verdict in {"excellent", "good"} or self.user_override


@dataclass(slots=True)
class CompareConfig:
    """Everything the comparison can be tuned by, snapshotted into the run."""

    dpi: int = 200
    tolerance: ToleranceSpec = field(default_factory=ToleranceSpec)
    text: TextDiffConfig = field(default_factory=TextDiffConfig)
    vector: VectorDiffConfig = field(default_factory=VectorDiffConfig)
    hatch: HatchConfig = field(default_factory=HatchConfig)
    raster: RasterDiffConfig = field(default_factory=RasterDiffConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    run_vector: bool = True
    run_raster: bool = True
    run_text: bool = True
    #: Change regions closer than this on paper are one region.
    cluster_paper_mm: float = 5.0
    #: Overlap at which two streams are describing the same change.
    dedupe_iou: float = 0.4
    #: Per-pair budget. On timeout the partial result says what is missing.
    timeout_s: float = 60.0
    #: Per-sheet tolerance override, when the user set one.
    override: SheetToleranceOverride | None = None

    def snapshot(self) -> dict[str, Any]:
        """A reproducible record of the settings this run used."""
        return {
            "engine_version": COMPARE_ENGINE_VERSION,
            "dpi": self.dpi,
            "tolerance": self.tolerance.as_dict(),
            "text": asdict(self.text),
            "vector": asdict(self.vector),
            "hatch": asdict(self.hatch),
            "raster": asdict(self.raster),
            "filters": asdict(self.filters),
            "run_vector": self.run_vector,
            "run_raster": self.run_raster,
            "run_text": self.run_text,
            "cluster_paper_mm": self.cluster_paper_mm,
            "dedupe_iou": self.dedupe_iou,
            "timeout_s": self.timeout_s,
            "override": asdict(self.override) if self.override else None,
        }


@dataclass(slots=True)
class ComparisonResult:
    """One pair compared: what changed, what was hidden, and why."""

    pair_id: str = ""
    changes: list[ChangeRecord] = field(default_factory=list)
    filtered: list[FilteredChange] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    stream_stats: list[StreamStats] = field(default_factory=list)
    tolerance: ResolvedTolerance | None = None
    mask: MaskSet | None = None
    #: The amendment table's text: read, never diffed.
    revision_rows: list[RevisionRow] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    engine_version: str = COMPARE_ENGINE_VERSION

    @property
    def reportable(self) -> list[ChangeRecord]:
        """What a user sees by default: everything not marked cosmetic."""
        return [change for change in self.changes if not change.is_cosmetic]

    @property
    def cosmetic(self) -> list[ChangeRecord]:
        return [change for change in self.changes if change.is_cosmetic]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "changes": len(self.changes),
            "reportable": len(self.reportable),
            "cosmetic": len(self.cosmetic),
            "filtered": len(self.filtered),
            "warnings": [warning.as_dict() for warning in self.warnings],
            "streams": [stat.as_dict() for stat in self.stream_stats],
            "tolerance": self.tolerance.as_dict() if self.tolerance else None,
            "revision_rows": [row.as_dict() for row in self.revision_rows],
            "timings": {name: round(value, 3) for name, value in self.timings.items()},
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "engine_version": self.engine_version,
        }


# ── Loading one sheet ───────────────────────────────────────────────────


@dataclass(slots=True)
class LoadedSheet:
    """A sheet rendered, read and ready to compare."""

    view: SheetView
    colour: NDArray[np.uint8] | None = None
    #: Where PDF annotations painted, from the Phase 4 two-pass render.
    annotations_mask: NDArray[np.uint8] | None = None
    vector_paths: list[NormalisedPath] = field(default_factory=list)
    layers: list[LayerInfo] = field(default_factory=list)
    vector_skip_reason: str = ""


def load_sheet(source: SheetSource, config: CompareConfig) -> LoadedSheet:
    """Render and read one sheet once, for every stream to share."""
    options = RenderOptions(dpi=config.dpi, colour=True, separate_annotations=True)
    render = render_page(source.path, source.page_index, options)

    with open_document(source.path) as document:
        page_text = extract_page_text(document[source.page_index], source.page_index)

    view = SheetView(
        page_text=page_text,
        gray=render.grayscale,
        width_px=render.width_px,
        height_px=render.height_px,
        dpi=config.dpi,
        scale_denominator=source.scale_denominator,
        scale_text=source.scale_text,
        source_path=source.path,
        page_index=source.page_index,
    )
    return LoadedSheet(view=view, colour=render.colour, annotations_mask=render.annotations_mask)


def build_sheet_mask(
    sheet: LoadedSheet, annotations_mask: NDArray[np.uint8] | None = None
) -> tuple[MaskSet, list[RevisionRow]]:
    """Detect everything that must not be compared on one sheet."""
    zones = detect_zones(sheet.view)
    detections = detect_watermarks(sheet.view) + detect_stamps(sheet.view)
    if annotations_mask is not None:
        detections += detect_annotations(sheet.view, annotations_mask)

    mask = build_mask(sheet.view)
    mask.zones.extend(zones.zones)
    mask.protected.extend(zones.protected)
    mask.zones.extend(detections_to_zones(detections))
    return mask, zones.revision_rows


def merge_masks(first: MaskSet, second: MaskSet) -> MaskSet:
    """Both sheets' exclusions apply to both sides.

    A watermark on one issue and not the other is precisely the case that has
    to be masked on both, or the comparison reports the watermark itself.
    """
    merged = MaskSet(name="pair", template_id=first.template_id or second.template_id)
    merged.zones = [*first.zones, *second.zones]
    merged.protected = [*first.protected, *second.protected]
    return merged


# ── Context for the classifier ──────────────────────────────────────────


def build_classify_context(
    paths: list[NormalisedPath], tolerance: ResolvedTolerance
) -> ClassifyContext:
    """Give the text classifier the geometry it needs to be sure.

    Two cheap, high-value pieces of context:

    * **Enclosures** — circles and rectangles that wrap a piece of text. `A`
      inside a circle is a grid reference; `A` on its own is nothing.
    * **Dimension lines** — a line with tick or arrow terminators at both
      ends. A number sitting on one is a dimension beyond doubt, and
      dimensions are the changes that cost money.
    """
    enclosures = [
        path.bbox for path in paths if path.geometry_type in {"circle", "rectangle"} and path.closed
    ]
    return ClassifyContext(
        dimension_lines=detect_dimension_lines(paths, tolerance),
        enclosures=enclosures,
        px_per_mm=tolerance.px_per_mm,
    )


#: A terminator tick is at most this long on paper.
TERMINATOR_PAPER_MM = 4.0
#: And sits at most this far from the line's end.
TERMINATOR_REACH_PAPER_MM = 2.0
#: A dimension line is at least this long on paper.
MIN_DIMENSION_LINE_PAPER_MM = 8.0


def detect_dimension_lines(paths: list[NormalisedPath], tolerance: ResolvedTolerance) -> list[Bbox]:
    """Lines with a terminator at each end — the shape of a dimension line."""
    px_per_mm = tolerance.px_per_mm
    terminator_limit = TERMINATOR_PAPER_MM * px_per_mm
    reach = TERMINATOR_REACH_PAPER_MM * px_per_mm
    minimum = MIN_DIMENSION_LINE_PAPER_MM * px_per_mm

    ticks = [path for path in paths if path.point_count == 2 and path.length <= terminator_limit]
    if not ticks:
        return []

    found: list[Bbox] = []
    for path in paths:
        if path.point_count != 2 or path.length < minimum:
            continue
        start, end = path.points[0], path.points[-1]
        at_start = any(_touches(tick, start, reach) for tick in ticks)
        at_end = any(_touches(tick, end, reach) for tick in ticks)
        if at_start and at_end:
            found.append(path.bbox)
    return found


def _touches(tick: NormalisedPath, point: tuple[float, float], reach: float) -> bool:
    return tick.bbox.expanded(reach).contains_point(point[0], point[1])


# ── The pipeline ────────────────────────────────────────────────────────


def compare_pair(
    pair: PairInput,
    alignment: AlignmentInput,
    config: CompareConfig | None = None,
) -> ComparisonResult:
    """Compare one pair of sheets, all streams, filtered and merged."""
    config = config or CompareConfig()
    started = time.perf_counter()
    deadline = started + config.timeout_s
    result = ComparisonResult(pair_id=pair.pair_id, config_snapshot=config.snapshot())

    # 1. Refuse to compare what did not align.
    if not alignment.proceeds:
        result.skipped = True
        result.skip_reason = (
            f"These two sheets did not align well enough to compare "
            f"(alignment verdict: {alignment.verdict}). Set the alignment points "
            "manually, or compare them side by side."
        )
        result.duration_s = time.perf_counter() - started
        logger.info("Comparison skipped | pair={} | {}", pair.pair_id, result.skip_reason)
        return result

    stage = _Timer(result.timings)

    with stage("load"):
        old_sheet = load_sheet(pair.old, config)
        new_sheet = load_sheet(pair.new, config)

    # 2. Tolerances, from this sheet's own drawing scale.
    with stage("tolerance"):
        tolerance = resolve_for_sheet(
            config.tolerance,
            scale_text=pair.new.scale_text,
            scale_denominator=pair.new.scale_denominator,
            dpi=config.dpi,
            override=config.override,
        )
        result.tolerance = tolerance
        if tolerance.note:
            result.warnings.append(
                Warning_(code="tolerance", message=tolerance.note, detail=tolerance.as_dict())
            )

    # 3. The mask: template zones, detections and the user's own.
    with stage("mask"):
        old_mask, _ = build_sheet_mask(old_sheet, old_sheet.annotations_mask)
        new_mask, revision_rows = build_sheet_mask(new_sheet, new_sheet.annotations_mask)
        mask = merge_masks(old_mask, new_mask)
        result.mask = mask
        result.revision_rows = revision_rows

    matrix = _matrix_for_dpi(alignment, config.dpi)
    changes: list[ChangeRecord] = []

    # 5/6. Geometry first, so the text classifier can use it.
    vector_paths_old: list[NormalisedPath] = []
    vector_paths_new: list[NormalisedPath] = []
    vector_stats = StreamStats(Stream.VECTOR)
    hatch_stats = StreamStats(Stream.HATCH)
    layers_old: list[LayerInfo] = []
    layers_new: list[LayerInfo] = []

    if config.run_vector and not _expired(deadline):
        with stage("vector"):
            vector_paths_old, vector_paths_new, layers_old, layers_new, skip = _load_vectors(
                pair, config, tolerance, matrix
            )
            vector_stats.old_item_count = len(vector_paths_old)
            vector_stats.new_item_count = len(vector_paths_new)
            vector_stats.skip_reason = skip
            vector_stats.ran = not skip

    # 4. The text stream. Always.
    text_result = None
    text_stats = StreamStats(Stream.TEXT)
    if config.run_text and not _expired(deadline):
        with stage("text"):
            context = build_classify_context(vector_paths_new, tolerance)
            old_items, dropped_old = prepare_items(
                old_sheet.view,
                mask=mask,
                transform=matrix,
                context=build_classify_context(vector_paths_old, tolerance),
                config=config.text,
                side="old",
            )
            new_items, dropped_new = prepare_items(
                new_sheet.view, mask=mask, context=context, config=config.text, side="new"
            )
            text_result = diff_text(old_items, new_items, tolerance, config=config.text)
            text_stats.ran = True
            text_stats.old_item_count = len(old_items)
            text_stats.new_item_count = len(new_items)
            text_stats.change_count = len(text_result.changes)
            changes.extend(text_result.changes)
            logger.debug("Text stream | masked old={} new={}", len(dropped_old), len(dropped_new))

    # 6. Hatch, inside the vector stream, before the general path diff.
    geometry_changes: list[ChangeRecord] = []
    if vector_stats.ran and not _expired(deadline):
        with stage("hatch"):
            old_regions = detect_hatch_regions(vector_paths_old, tolerance, config.hatch)
            new_regions = detect_hatch_regions(vector_paths_new, tolerance, config.hatch)
            hatch_changes = diff_hatch(old_regions, new_regions, tolerance, config.hatch)
            hatch_stats.ran = True
            hatch_stats.old_item_count = len(old_regions)
            hatch_stats.new_item_count = len(new_regions)
            hatch_stats.change_count = len(hatch_changes)
            geometry_changes.extend(hatch_changes)

            # Each side drops its own regions' segments, and anything short
            # sitting inside the *other* side's regions too: a hatch that was
            # removed leaves a region on one sheet only, and its segments
            # must not come back as individual removals on the other.
            vector_paths_old = exclude_hatch_paths(
                vector_paths_old, old_regions, tolerance, config.hatch, new_regions
            )
            vector_paths_new = exclude_hatch_paths(
                vector_paths_new, new_regions, tolerance, config.hatch, old_regions
            )

        with stage("vector_diff"):
            vector_result = diff_vectors(
                vector_paths_old,
                vector_paths_new,
                tolerance,
                mask=mask,
                page_px=(new_sheet.view.width_px, new_sheet.view.height_px),
                config=config.vector,
            )
            vector_stats.change_count = len(vector_result.changes)
            geometry_changes.extend(vector_result.changes)

    # 7. The raster stream, always, as the safety net.
    raster_stats = StreamStats(Stream.RASTER)
    raster_result = None
    if config.run_raster and not _expired(deadline):
        with stage("raster"):
            raster_result = diff_raster(
                old_sheet.view.gray,
                new_sheet.view.gray,
                matrix,
                tolerance,
                mask=mask,
                config=config.raster,
            )
            raster_stats.ran = raster_result.ran
            raster_stats.skip_reason = raster_result.skip_reason
            raster_stats.change_count = len(raster_result.changes)
            geometry_changes.extend(raster_result.changes)

    changes.extend(geometry_changes)

    # 8/9. Residual detection and the noise filter suite.
    with stage("filters"):
        sheet_area = float(new_sheet.view.width_px * new_sheet.view.height_px)
        optional_content_bbox = _optional_content_bbox(vector_paths_old, vector_paths_new)
        outcome = run_filters(
            changes,
            tolerance=tolerance,
            sheet_area_px=sheet_area,
            old_stroke_px=raster_result.old_stroke_width_px if raster_result else 0.0,
            new_stroke_px=raster_result.new_stroke_width_px if raster_result else 0.0,
            unchanged_text_boxes=text_result.unchanged_boxes if text_result else [],
            old_layers=layers_old,
            new_layers=layers_new,
            old_colour=old_sheet.colour,
            new_colour=new_sheet.colour,
            old_binary=raster_result.old_binary if raster_result else None,
            new_binary=raster_result.new_binary if raster_result else None,
            change_mask=raster_result.change_mask if raster_result else None,
            optional_content_bbox=optional_content_bbox,
            config=config.filters,
        )
        result.filtered.extend(outcome.filtered)
        result.warnings.extend(outcome.warnings)
        changes = outcome.changes

    # 10. One physical change seen by two streams is one change.
    with stage("dedupe"):
        changes = deduplicate(changes, config.dedupe_iou)

    # 11. Dimensions against geometry, in both directions.
    with stage("cross_check"):
        cross_check(changes, text_result, tolerance, config)

    # 12. Primitive clustering of geometry regions. Semantics are Phase 6.
    with stage("cluster"):
        changes = cluster_regions(changes, tolerance, config.cluster_paper_mm)

    result.changes = changes
    result.stream_stats = [text_stats, vector_stats, hatch_stats, raster_stats]
    result.timed_out = _expired(deadline)
    if result.timed_out:
        result.warnings.append(
            Warning_(
                code="timeout",
                message=(
                    f"This comparison ran past its {config.timeout_s:.0f} second budget, so "
                    "some of it did not finish. What is shown is complete for the streams "
                    "that did run."
                ),
                detail={"timings": dict(result.timings)},
            )
        )
    result.duration_s = time.perf_counter() - started

    logger.info(
        "Compared | pair={} | reportable={} | cosmetic={} | filtered={} | {:.2f}s",
        pair.pair_id,
        len(result.reportable),
        len(result.cosmetic),
        len(result.filtered),
        result.duration_s,
    )
    return result


def compare_batch(
    pairs: list[tuple[PairInput, AlignmentInput]],
    config: CompareConfig | None = None,
    progress_callback: Any | None = None,
    *,
    workers: int = 4,
    should_cancel: Any | None = None,
    already_done: set[str] | None = None,
) -> list[ComparisonResult]:
    """Compare many pairs, in parallel, resumably and cancellably.

    Parallelism comes from a process pool because **pdfium is not
    thread-safe**: every worker gets its own copy and its own lock, which is
    the same arrangement Phase 4 uses. ``already_done`` lets a cancelled run
    resume without redoing finished pairs, and ``should_cancel`` is polled
    between pairs so a cancel takes effect within one comparison rather than
    at the end of the batch.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    config = config or CompareConfig()
    done = already_done or set()
    todo = [(pair, alignment) for pair, alignment in pairs if pair.pair_id not in done]
    results: list[ComparisonResult] = []
    total = len(todo)

    if workers <= 1 or total <= 1:
        for index, (pair, alignment) in enumerate(todo):
            if should_cancel is not None and should_cancel():
                break
            results.append(compare_pair(pair, alignment, config))
            if progress_callback is not None:
                progress_callback(index + 1, total, pair.label or pair.pair_id)
        return results

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(compare_pair, pair, alignment, config): pair for pair, alignment in todo
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            pair = futures[future]
            if should_cancel is not None and should_cancel():
                for pending in futures:
                    pending.cancel()
                break
            try:
                results.append(future.result())
            except Exception as exc:  # one bad pair must not stop the whole batch
                logger.exception("Comparison failed | pair={} | {}", pair.pair_id, exc)
                results.append(
                    ComparisonResult(
                        pair_id=pair.pair_id,
                        skipped=True,
                        skip_reason=(
                            "This pair could not be compared. The details are in the log file."
                        ),
                    )
                )
            if progress_callback is not None:
                progress_callback(completed, total, pair.label or pair.pair_id)

    return results


class _Timer:
    """Times each stage into the result's timing map."""

    def __init__(self, timings: dict[str, float]) -> None:
        self._timings = timings
        self._name = ""
        self._started = 0.0

    def __call__(self, name: str) -> _Timer:
        self._name = name
        return self

    def __enter__(self) -> _Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._timings[self._name] = time.perf_counter() - self._started


def _expired(deadline: float) -> bool:
    return time.perf_counter() > deadline


def _matrix_for_dpi(alignment: AlignmentInput, dpi: int) -> NDArray[np.float64] | None:
    """Rescale the alignment matrix from its own DPI to the comparison DPI.

    The matrix maps pixels to pixels, so changing the resolution changes it:
    ``M' = S M S⁻¹`` with ``S`` the scale between the two resolutions. Getting
    this wrong shifts the whole old sheet by a fraction of its own size, and
    every stroke on it then reports as changed.
    """
    if alignment.matrix is None:
        return None
    matrix = np.asarray(alignment.matrix, dtype=float)
    if alignment.dpi == dpi or alignment.dpi <= 0:
        return matrix
    scale = dpi / alignment.dpi
    scaling = np.diag([scale, scale, 1.0])
    inverse = np.diag([1.0 / scale, 1.0 / scale, 1.0])
    return scaling @ matrix @ inverse


def _load_vectors(
    pair: PairInput,
    config: CompareConfig,
    tolerance: ResolvedTolerance,
    matrix: NDArray[np.float64] | None,
) -> tuple[list[NormalisedPath], list[NormalisedPath], list[LayerInfo], list[LayerInfo], str]:
    """Extract, warp and normalise both sides' geometry."""
    old_page = extract_vector_page(pair.old.path, pair.old.page_index, dpi=config.dpi)
    new_page = extract_vector_page(pair.new.path, pair.new.page_index, dpi=config.dpi)

    if not old_page.usable or not new_page.usable:
        reason = old_page.skip_reason or new_page.skip_reason or "No usable geometry."
        return [], [], old_page.layers, new_page.layers, reason

    grid = max(tolerance.position.px * 0.5, 0.05)
    old_paths = normalise_paths(old_page.paths, tolerance_px=grid, px_per_mm=tolerance.px_per_mm)
    new_paths = normalise_paths(new_page.paths, tolerance_px=grid, px_per_mm=tolerance.px_per_mm)

    if matrix is not None:
        listed = [[float(value) for value in row] for row in np.asarray(matrix, dtype=float)]
        for path in old_paths:
            path.points = transform_points(path.points, listed)
            path.bbox = Bbox.from_points(path.points)

    return old_paths, new_paths, old_page.layers, new_page.layers, ""


# ── Merging results ─────────────────────────────────────────────────────


def _optional_content_bbox(
    old_paths: list[NormalisedPath], new_paths: list[NormalisedPath]
) -> Bbox | None:
    """Where the sheet's optional content sits, across both issues.

    A layer switched off exists on one sheet and not the other, so the box
    has to come from whichever side still carries it.
    """
    boxes = [path.bbox for path in (*old_paths, *new_paths) if path.in_optional_content]
    if not boxes:
        return None
    box = boxes[0]
    for other in boxes[1:]:
        box = box.union(other)
    return box


#: Records that describe the whole sheet rather than one place on it. Merging
#: them with a localised finding would turn a deliberate cosmetic verdict into
#: a reportable change, which is how a toggled layer came back as a change.
SHEET_LEVEL_KINDS = frozenset(
    {
        ChangeKind.LAYER_VISIBILITY_CHANGED,
        ChangeKind.BACKGROUND_UPDATED,
        ChangeKind.TAGS_RENUMBERED,
        ChangeKind.STYLE_ONLY,
    }
)


def deduplicate(changes: list[ChangeRecord], iou_threshold: float) -> list[ChangeRecord]:
    """One physical change found by two streams becomes one record.

    The record with the most informative stream wins the description — a text
    change beats a hatch region beats a vector path beats a raster blob — and
    gains confidence, because two independent methods agreeing is a stronger
    statement than either alone.

    The threshold is deliberately tight. When in doubt the two records are
    kept: merging two genuinely separate changes hides one of them, which is
    worse than reporting the same thing twice.
    """
    ordered = sorted(changes, key=lambda change: (change.primary_stream.rank, -change.bbox.area))
    merged: list[ChangeRecord] = []

    for change in ordered:
        target = None
        if change.kind in SHEET_LEVEL_KINDS:
            merged.append(change)
            continue
        for candidate in merged:
            if candidate.primary_stream is change.primary_stream:
                continue  # same stream: two findings, not one seen twice
            if candidate.kind in SHEET_LEVEL_KINDS:
                continue
            if _same_change(candidate, change, iou_threshold):
                target = candidate
                break
        if target is None:
            merged.append(change)
            continue

        for stream in change.streams:
            target.with_stream(stream)
        target.bbox = target.bbox.union(change.bbox)
        target.confidence = min(0.99, target.confidence + 0.1)
        # A cosmetic finding from one stream does not make a real finding from
        # a better one cosmetic; the other way round it does.
        target.is_cosmetic = target.is_cosmetic and change.is_cosmetic
        if target.hatch is None and change.hatch is not None:
            target.hatch = change.hatch
        if target.vector is None and change.vector is not None:
            target.vector = change.vector

    return merged


def _same_change(first: ChangeRecord, second: ChangeRecord, iou_threshold: float) -> bool:
    if first.bbox.iou(second.bbox) >= iou_threshold:
        return True
    # A text change is a small box; the raster blob covering the same words is
    # larger and contains it. Containment counts, overlap fraction does not.
    smaller, larger = sorted((first, second), key=lambda change: change.bbox.area)
    if smaller.bbox.area <= 0:
        return False
    return smaller.bbox.intersection_area(larger.bbox) / smaller.bbox.area >= 0.8


def cross_check(
    changes: list[ChangeRecord],
    text_result: Any,
    tolerance: ResolvedTolerance,
    config: CompareConfig,
) -> None:
    """Dimensions against geometry, both ways round."""
    radius = max(tolerance.paper_mm_to_px(10.0), tolerance.position.px * 4.0)
    geometry = [
        change
        for change in changes
        if change.primary_stream in {Stream.VECTOR, Stream.RASTER, Stream.HATCH}
        and not change.is_cosmetic
    ]

    for change in changes:
        if change.text is None or change.primary_stream is not Stream.TEXT:
            continue
        if change.text.category not in {TextCategory.DIMENSION, TextCategory.LEVEL}:
            continue
        check = cross_check_geometry(change, geometry, radius)
        change.text.cross_check = check.flag
        if check.flag is CrossCheckFlag.DIMENSION_TEXT_ONLY:
            change.description = f"{change.description} — {check.message}"
        impact = estimate_quantity_impact(change, geometry, radius)
        if impact is not None:
            change.detail["quantity_impact"] = impact.as_dict()

    if text_result is None:
        return
    for box, text, check in find_stale_dimensions(
        geometry, text_result.unchanged_dimensions, radius
    ):
        changes.append(
            ChangeRecord(
                kind=ChangeKind.MODIFIED,
                bbox=box,
                streams=[Stream.TEXT, Stream.VECTOR],
                description=check.message,
                confidence=0.6,
                text=TextChangeDetail(
                    category=TextCategory.DIMENSION,
                    old_text=text,
                    new_text=text,
                    cross_check=check.flag,
                ),
            )
        )


def cluster_regions(
    changes: list[ChangeRecord], tolerance: ResolvedTolerance, cluster_paper_mm: float
) -> list[ChangeRecord]:
    """Merge nearby geometry regions. Text changes are never clustered.

    Eight lines deleted inside one room are one region a user will look at
    once. Two dimensions that changed in the same corner are two findings,
    each with its own old and new value, and collapsing them would throw away
    the only part that matters.
    """
    radius = cluster_paper_mm * tolerance.px_per_mm
    clusterable = [
        change
        for change in changes
        if change.text is None
        and change.hatch is None
        and change.kind
        not in {
            ChangeKind.LAYER_VISIBILITY_CHANGED,
            ChangeKind.BACKGROUND_UPDATED,
            ChangeKind.TAGS_RENUMBERED,
        }
    ]
    others = [change for change in changes if change not in clusterable]
    if len(clusterable) < 2:
        return changes

    groups: list[list[ChangeRecord]] = []
    for change in clusterable:
        for group in groups:
            if any(
                member.bbox.expanded(radius).intersects(change.bbox)
                and member.is_cosmetic == change.is_cosmetic
                for member in group
            ):
                group.append(change)
                break
        else:
            groups.append([change])

    clustered: list[ChangeRecord] = []
    for group in groups:
        if len(group) == 1:
            clustered.append(group[0])
            continue
        clustered.append(_merge_group(group, tolerance))

    return [*others, *clustered]


def _merge_group(group: list[ChangeRecord], tolerance: ResolvedTolerance) -> ChangeRecord:
    box = group[0].bbox
    for change in group[1:]:
        box = box.union(change.bbox)
    kinds = {change.kind for change in group}
    kind = next(iter(kinds)) if len(kinds) == 1 else ChangeKind.MODIFIED
    streams = sorted(
        {stream for change in group for stream in change.streams}, key=lambda item: item.rank
    )

    width_mm = tolerance.px_to_site_mm(box.w)
    size = f"{width_mm / 1000:.1f} m across on site" if width_mm else f"{box.w:.0f} px across"
    verb = {
        ChangeKind.ADDED: "added",
        ChangeKind.REMOVED: "removed",
        ChangeKind.MOVED: "moved",
    }.get(kind, "changed")

    return ChangeRecord(
        kind=kind,
        bbox=box,
        streams=list(streams),
        description=f"{len(group)} objects {verb} in one area ({size})",
        confidence=max(change.confidence for change in group),
        is_cosmetic=all(change.is_cosmetic for change in group),
        detail={"clustered_from": len(group)},
    )
