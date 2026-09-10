"""Stage 6, assembled: compare one aligned pair and return its changes.

The order is fixed and each step is separately testable: render both
sheets, mask the title block, diff the ink, cluster what differs, type and
rank each cluster.

Two rules from the plan shape everything here. A sheet whose only change is
its revision letter must report zero changes - that is what the title block
mask is for. And the pipeline never invents confidence: when it cannot
render, cannot read the scale, or is handed no transform for sheets that
are not the same size, it says so in :attr:`CompareResult.failure` rather
than returning a diff nobody should trust.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.align.units import mm_to_px
from engine.compare.change_typer import build_regions
from engine.compare.cluster import Box, cluster_regions, sort_boxes
from engine.compare.exclusions import ExclusionZone, build_mask, title_block_zone
from engine.compare.raster_diff import diff_masks
from engine.compare.text_diff import TextChange, text_change_for
from engine.compare.types import CompareConfig, CompareResult
from engine.extract.raster_renderer import RenderOptions, render_page
from engine.extract.text_extractor import PageText, extract_page_text
from engine.utils.errors import UnreadableFileError, ValidationError
from engine.utils.pdf_runtime import open_document


def _render_grayscale(
    path: str,
    page_index: int,
    config: CompareConfig,
) -> tuple[NDArray[np.uint8], float]:
    """The sheet as grayscale at the comparison DPI, plus its paper width."""
    result = render_page(
        path,
        page_index,
        RenderOptions(dpi=config.dpi, colour=False, grayscale_for_compare=True),
    )
    if result.grayscale is None:
        raise UnreadableFileError(
            "That sheet could not be rendered for comparison.",
            detail={"path": path, "page_index": page_index},
        )
    return result.grayscale, result.mm_width


def _page_text(path: str, page_index: int) -> PageText | None:
    """The text layer of one page, or None when it has none or cannot open."""
    try:
        with open_document(path) as document:
            if page_index < 0 or page_index >= len(document):
                return None
            return extract_page_text(document[page_index], page_index)
    except (UnreadableFileError, ValidationError, OSError):
        return None


def exclusion_zones(path: str, page_index: int, *, mask_title_block: bool) -> list[ExclusionZone]:
    """The zones to ignore on one sheet."""
    if not mask_title_block:
        return []
    zone = title_block_zone(_page_text(path, page_index))
    return [zone] if zone is not None else []


def _old_box(box: Box, inverse: NDArray[np.float64] | None) -> Box:
    """A box on the new grid, expressed on the old sheet's grid.

    The old sheet's text sits in the old sheet's coordinates, so looking a
    region up there without undoing the alignment would read the wrong part
    of the drawing on any pair that was rotated or rescaled.
    """
    if inverse is None:
        return box
    x, y, w, h = box
    corners = np.array(
        [[x, y, 1.0], [x + w, y, 1.0], [x, y + h, 1.0], [x + w, y + h, 1.0]],
        dtype=np.float64,
    )
    mapped = corners @ inverse[:2, :].T
    x0, y0 = mapped.min(axis=0)
    x1, y1 = mapped.max(axis=0)
    return (int(x0), int(y0), max(1, int(x1 - x0)), max(1, int(y1 - y0)))


def _text_lookup(
    old_path: str,
    old_page_index: int,
    new_path: str,
    new_page_index: int,
    matrix: NDArray[np.float64] | None,
    config: CompareConfig,
    old_size: tuple[int, int],
    new_size: tuple[int, int],
) -> Callable[[Box], TextChange | None] | None:
    """A reader for what each region says, or None when neither sheet has text.

    The pages are read once and closed; the returned function only does
    geometry after that, so a sheet with hundreds of regions still opens
    each document exactly once.

    *old_size* and *new_size* are the rendered raster dimensions in pixels,
    which is the space every box is in - not the page box in points.
    """
    old_page = _page_text(old_path, old_page_index)
    new_page = _page_text(new_path, new_page_index)
    if (old_page is None or old_page.is_empty) and (new_page is None or new_page.is_empty):
        return None

    inverse: NDArray[np.float64] | None = None
    if matrix is not None:
        try:
            inverse = np.linalg.inv(np.asarray(matrix, dtype=np.float64))
        except np.linalg.LinAlgError:
            inverse = None

    pad = max(1, round(mm_to_px(config.merge_gap_mm, config.dpi)))

    def lookup(box: Box) -> TextChange | None:
        return text_change_for(
            box,
            old_page,
            new_page,
            new_size,
            old_size,
            pad,
            old_box=_old_box(box, inverse),
        )

    return lookup


def compare_pair(
    old_path: str,
    new_path: str,
    *,
    old_page_index: int = 0,
    new_page_index: int = 0,
    matrix: NDArray[np.float64] | None = None,
    config: CompareConfig | None = None,
    scale_denominator: int | None = None,
    mask_title_block: bool = True,
) -> CompareResult:
    """Find the changes between one aligned pair of sheets.

    *matrix* is the row-major 3x3 Phase 4 fitted, mapping old pixels to new
    pixels at *config.dpi*. Passing None is only honest when the two sheets
    are already on the same grid; if they are not, the result comes back
    with a failure rather than a diff of two mismatched rasters.
    """
    cfg = config if config is not None else CompareConfig()
    started = time.perf_counter()

    result = CompareResult(
        old_path=old_path,
        new_path=new_path,
        old_page_index=old_page_index,
        new_page_index=new_page_index,
        dpi=cfg.dpi,
        width_px=0,
        height_px=0,
        scale_denominator=scale_denominator,
    )

    try:
        old_gray, _ = _render_grayscale(old_path, old_page_index, cfg)
        new_gray, _ = _render_grayscale(new_path, new_page_index, cfg)
    except (UnreadableFileError, ValidationError) as exc:
        result.failure = str(exc)
        result.duration_s = time.perf_counter() - started
        return result

    result.height_px, result.width_px = new_gray.shape[:2]

    if matrix is None and old_gray.shape[:2] != new_gray.shape[:2]:
        result.failure = (
            "These sheets are different sizes and no alignment was supplied, "
            "so they cannot be compared. Align the pair first."
        )
        result.duration_s = time.perf_counter() - started
        return result

    zones = exclusion_zones(new_path, new_page_index, mask_title_block=mask_title_block)
    mask = build_mask(zones, result.width_px, result.height_px)

    masks = diff_masks(old_gray, new_gray, matrix, cfg, exclusion_mask=mask)
    result.total_added_px = masks.added_px
    result.total_removed_px = masks.removed_px

    boxes = sort_boxes(cluster_regions(masks, cfg))
    if len(boxes) > cfg.max_regions:
        boxes = boxes[: cfg.max_regions]
        result.truncated = True

    result.regions = build_regions(
        boxes,
        masks,
        cfg,
        scale_denominator,
        _text_lookup(
            old_path,
            old_page_index,
            new_path,
            new_page_index,
            matrix,
            cfg,
            old_size=(old_gray.shape[1], old_gray.shape[0]),
            new_size=(result.width_px, result.height_px),
        ),
    )
    result.duration_s = time.perf_counter() - started

    logger.info(
        "Compared {} -> {}: {} region(s) ({} substantive) in {:.1f} s",
        old_path,
        new_path,
        len(result.regions),
        result.substantive_count,
        result.duration_s,
    )
    return result
