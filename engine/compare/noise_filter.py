"""Task 5.12 — the noise filters. The part that decides whether anyone uses this.

The comparison algorithms are the interesting engineering. These are the
product. A tool that shows twelve changes and explains that it suppressed
four hundred cosmetic differences is trustworthy; a tool that shows four
hundred and twelve is not, no matter how correct every one of them is.

Each filter targets one specific, common false positive and says in words
what it did:

* a heavier pen on the replot,
* a dashed line type where there was a solid one,
* a colour plot against a monochrome one,
* a substituted font where the text is provably identical,
* a whole layer switched off,
* speckle from anti-aliasing or a dirty scanner glass,
* a background or xref updated wholesale,
* and the one that matters most — **alignment residual**.

Nothing is ever discarded silently. A filter either marks a change cosmetic,
in which case it stays in the list and is hidden by default, or it moves it
to the run's filtered list with a reason the user can read. "What did you
hide, and why?" must always have an answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from loguru import logger
from numpy.typing import NDArray

from engine.compare.tolerance import ResolvedTolerance
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    FilteredChange,
    Stream,
    Warning_,
)
from engine.extract.vector_extractor import LayerInfo

#: Stroke widths differing by more than this are a different pen.
STROKE_WIDTH_DIFFERENCE = 0.20
#: This share of paths differing only in style means the sheet was replotted.
REPLOT_SHARE = 0.5
#: A region bigger than this share of the sheet is a background, not an edit.
BACKGROUND_SHARE = 0.15
#: Components thinner than this are strokes traced by alignment error.
RESIDUAL_MAX_WIDTH_PX = 3.0
#: Above this share of thin tracing components, the map is residual.
RESIDUAL_SHARE = 0.70
#: A lone component with no neighbour this close is speckle.
SPECKLE_NEIGHBOUR_PAPER_MM = 5.0


@dataclass(slots=True)
class FilterConfig:
    """Every threshold the suite uses."""

    stroke_width_difference: float = STROKE_WIDTH_DIFFERENCE
    replot_share: float = REPLOT_SHARE
    background_share: float = BACKGROUND_SHARE
    residual_max_width_px: float = RESIDUAL_MAX_WIDTH_PX
    residual_share: float = RESIDUAL_SHARE
    speckle_neighbour_paper_mm: float = SPECKLE_NEIGHBOUR_PAPER_MM
    #: Minimum component area in pixels, on top of the tolerance minimum.
    min_component_px: int = 6


@dataclass(slots=True)
class FilterOutcome:
    """What came out of the suite: kept changes, and what was suppressed."""

    changes: list[ChangeRecord] = field(default_factory=list)
    filtered: list[FilteredChange] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    #: filter name -> how many records it suppressed or collapsed.
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, name: str, count: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + count

    def suppress(self, change: ChangeRecord, name: str, reason: str) -> None:
        self.filtered.append(FilteredChange(change=change, filter_name=name, reason=reason))
        self.record(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept": len(self.changes),
            "filtered": len(self.filtered),
            "counts": dict(self.counts),
            "warnings": [warning.as_dict() for warning in self.warnings],
        }


# ── Style filters ───────────────────────────────────────────────────────


def filter_line_weight(
    outcome: FilterOutcome,
    old_stroke_px: float,
    new_stroke_px: float,
    tolerance: ResolvedTolerance,
    config: FilterConfig | None = None,
) -> None:
    """A sheet replotted with heavier pens is not a sheet that changed.

    The two stroke width estimates come from the raster stream. When they
    differ materially, every style-only vector difference on the sheet is one
    fact — the pen changed — and is collapsed into a single record rather
    than one per line.
    """
    config = config or FilterConfig()
    measured = old_stroke_px > 0 and new_stroke_px > 0
    confirmed = False
    if measured:
        larger = max(old_stroke_px, new_stroke_px)
        confirmed = abs(old_stroke_px - new_stroke_px) / larger > config.stroke_width_difference

    if confirmed:
        old_mm = tolerance.px_to_paper_mm(old_stroke_px)
        new_mm = tolerance.px_to_paper_mm(new_stroke_px)
        message = (
            f"This sheet was replotted with a different pen weight "
            f"({old_mm:.2f} mm → {new_mm:.2f} mm on paper). Differences caused by the "
            "heavier line alone are not reported as changes."
        )
        outcome.warnings.append(
            Warning_(
                code="line_weight_changed",
                message=message,
                detail={"old_mm": old_mm, "new_mm": new_mm},
            )
        )
    else:
        # The vector stream can prove a pen change on its own: identical
        # geometry with a different stroke width, many times over. The
        # collapse must not wait for the raster estimate to agree — that
        # estimate is a median over the whole sheet and a change confined to
        # one pen never moves it far enough.
        message = (
            "Objects on this sheet were replotted with a different pen weight. The "
            "geometry is identical, so this is a plotting difference."
        )
    _collapse_style(outcome, "filter_line_weight", message, "line weight")


def filter_line_type(outcome: FilterOutcome, config: FilterConfig | None = None) -> None:
    """Same geometry, different dash pattern. Cosmetic by construction."""
    _collapse_style(
        outcome,
        "filter_line_type",
        "Line types changed on this sheet (solid against dashed). The geometry is "
        "identical, so this is a plotting difference, not a design change.",
        "line type",
    )


def _collapse_style(outcome: FilterOutcome, name: str, message: str, wording: str) -> None:
    """Turn many style-only records into one, when they say the same thing."""
    matching = [
        change
        for change in outcome.changes
        if change.kind is ChangeKind.STYLE_ONLY
        and change.vector is not None
        and wording in change.vector.style_difference
    ]
    if len(matching) < 2:
        return

    box = matching[0].bbox
    for change in matching[1:]:
        box = box.union(change.bbox)

    for change in matching:
        outcome.changes.remove(change)
        outcome.suppress(change, name, message)

    outcome.changes.append(
        ChangeRecord(
            kind=ChangeKind.STYLE_ONLY,
            bbox=box,
            streams=[Stream.VECTOR],
            description=f"{len(matching)} objects differ only in {wording}. {message}",
            confidence=0.9,
            is_cosmetic=True,
            detail={"collapsed_from": len(matching)},
        )
    )
    outcome.record(f"{name}_collapsed")


def filter_colour_plot(
    outcome: FilterOutcome,
    old_colour: NDArray[np.uint8] | None,
    new_colour: NDArray[np.uint8] | None,
    old_binary: NDArray[np.uint8] | None,
    new_binary: NDArray[np.uint8] | None,
) -> None:
    """A colour plot against a monochrome one of the same drawing.

    The test is the pair of facts together: the colour histograms differ
    substantially *and* the binarised images agree. Either on its own means
    nothing.
    """
    if old_colour is None or new_colour is None:
        return
    if old_binary is None or new_binary is None:
        return

    if not _histograms_differ(old_colour, new_colour):
        return
    if not _binaries_agree(old_binary, new_binary):
        return

    outcome.warnings.append(
        Warning_(
            code="plot_settings_changed",
            message=(
                "One issue was plotted in colour and the other in monochrome. The "
                "drawn content is the same, so the colour difference is not reported."
            ),
        )
    )
    for change in list(outcome.changes):
        if change.primary_stream is Stream.RASTER:
            outcome.changes.remove(change)
            outcome.suppress(
                change,
                "filter_colour_plot",
                "Colour plot against monochrome plot; the binarised drawings agree.",
            )


def _histograms_differ(first: NDArray[np.uint8], second: NDArray[np.uint8]) -> bool:
    if first.ndim < 3 or second.ndim < 3:
        return False
    saturation_first = cv2.cvtColor(first, cv2.COLOR_RGB2HSV)[:, :, 1]
    saturation_second = cv2.cvtColor(second, cv2.COLOR_RGB2HSV)[:, :, 1]
    return abs(float(saturation_first.mean()) - float(saturation_second.mean())) > 4.0


def _binaries_agree(first: NDArray[np.uint8], second: NDArray[np.uint8]) -> bool:
    if first.shape != second.shape:
        return False
    ink = (first > 0) | (second > 0)
    total = int(ink.sum())
    if total == 0:
        return True
    matching = int(((first > 0) & (second > 0)).sum())
    return matching / total > 0.95


def filter_font_substitution(
    outcome: FilterOutcome,
    unchanged_text_boxes: list[Bbox],
    tolerance: ResolvedTolerance,
) -> None:
    """Where the text stream proved a string identical, its pixels cannot differ.

    A font that was not installed at plot time re-renders every glyph. The
    text stream reads both sides as the same string, which is a stronger
    statement than any pixel comparison, so raster differences inside those
    strings are suppressed.
    """
    if not unchanged_text_boxes:
        return
    # Glyph ink overshoots the box a reader reports for it — descenders,
    # italic overhang, the heavier stem of a substituted face — so the margin
    # is a real distance on paper rather than a multiple of a tolerance that
    # is a quarter of a millimetre at 1:100.
    margin = max(tolerance.position.px * 2.0, tolerance.paper_mm_to_px(0.5))
    grown = [box.expanded(margin) for box in unchanged_text_boxes]

    for change in list(outcome.changes):
        if change.primary_stream is not Stream.RASTER:
            continue
        if not any(box.intersection_area(change.bbox) > change.bbox.area * 0.8 for box in grown):
            continue
        outcome.changes.remove(change)
        outcome.suppress(
            change,
            "filter_font_substitution",
            "This sits inside text the comparison read as identical on both sheets, "
            "so the difference is the font, not the wording.",
        )


def filter_layer_toggle(
    outcome: FilterOutcome,
    old_layers: list[LayerInfo],
    new_layers: list[LayerInfo],
    optional_content_bbox: Bbox | None = None,
    tolerance: ResolvedTolerance | None = None,
) -> None:
    """A layer switched off is one fact, not one change per object on it.

    ``optional_content_bbox`` is where the sheet's optional content actually
    sits, from the vector extractor. Every geometry finding inside it is
    accounted for by the toggle and is folded into the one record — which is
    the difference between "one layer was switched off" and four hundred
    lines reported as removed.
    """
    old_by_name = {layer.name: layer for layer in old_layers}
    new_by_name = {layer.name: layer for layer in new_layers}

    toggled: list[tuple[str, bool]] = []
    for name, layer in old_by_name.items():
        other = new_by_name.get(name)
        if other is None:
            if layer.visible:
                toggled.append((name, False))
            continue
        if layer.visible != other.visible:
            toggled.append((name, other.visible))
    for name, layer in new_by_name.items():
        if name not in old_by_name and layer.visible:
            toggled.append((name, True))

    if not toggled:
        return

    region = optional_content_bbox
    absorbed = 0
    if region is not None and region.area > 0:
        # The box comes from path geometry, which is the centreline. Ink on a
        # line drawn exactly on the boundary lands a pixel or two outside it,
        # so the last object on a layer would otherwise escape the toggle.
        margin = max(4.0, tolerance.position.px * 3.0 if tolerance else 4.0)
        region = region.expanded(margin)
        for change in list(outcome.changes):
            if change.primary_stream is Stream.TEXT:
                continue
            if change.bbox.area <= 0:
                continue
            inside = change.bbox.intersection_area(region) / change.bbox.area
            if inside < 0.8:
                continue
            outcome.changes.remove(change)
            absorbed += 1
            outcome.suppress(
                change,
                "filter_layer_toggle",
                "This sits on a layer whose visibility changed between the two issues, "
                "so it is reported once as the layer rather than object by object.",
            )
    else:
        region = _all_bbox(outcome.changes)

    for name, now_visible in toggled:
        state = "switched on" if now_visible else "switched off"
        detail_sentence = (
            f"Everything on it ({absorbed} findings) is reported as this one change."
            if absorbed
            else "Everything on it is reported as this one change."
        )
        outcome.changes.append(
            ChangeRecord(
                kind=ChangeKind.LAYER_VISIBILITY_CHANGED,
                bbox=region,
                streams=[Stream.VECTOR],
                description=(
                    f"The layer '{name}' was {state} between the two issues. {detail_sentence}"
                ),
                confidence=0.9,
                is_cosmetic=True,
                detail={"layer": name, "visible": now_visible, "absorbed": absorbed},
            )
        )
        outcome.record("filter_layer_toggle")
        logger.debug("Layer toggle | {} | now visible={}", name, now_visible)


def _all_bbox(changes: list[ChangeRecord]) -> Bbox:
    if not changes:
        return Bbox(0.0, 0.0, 0.0, 0.0)
    box = changes[0].bbox
    for change in changes[1:]:
        box = box.union(change.bbox)
    return box


# ── Size and shape filters ──────────────────────────────────────────────


def filter_speckle(
    outcome: FilterOutcome,
    tolerance: ResolvedTolerance,
    config: FilterConfig | None = None,
) -> None:
    """Anti-alias dust and scanner speckle: too small, or entirely alone."""
    config = config or FilterConfig()
    minimum = max(tolerance.min_region.px_area, float(config.min_component_px))
    neighbour = config.speckle_neighbour_paper_mm * tolerance.px_per_mm

    raster = [change for change in outcome.changes if change.primary_stream is Stream.RASTER]
    for change in list(raster):
        area = float(change.detail.get("pixel_area", change.bbox.area))
        if area >= minimum:
            continue
        others = [other for other in raster if other is not change]
        has_neighbour = any(
            other.bbox.expanded(neighbour).intersects(change.bbox) for other in others
        )
        if has_neighbour and area >= minimum * 0.5:
            continue
        outcome.changes.remove(change)
        raster.remove(change)
        outcome.suppress(
            change,
            "filter_speckle",
            f"Too small to be a drawn change "
            f"({tolerance.px_to_paper_mm(math.sqrt(max(area, 0.0))):.2f} mm on paper) "
            "and with nothing changed near it.",
        )


def filter_background_update(
    outcome: FilterOutcome,
    sheet_area_px: float,
    config: FilterConfig | None = None,
) -> None:
    """One huge coherent region is an updated background, not ten thousand edits."""
    config = config or FilterConfig()
    if sheet_area_px <= 0:
        return
    limit = sheet_area_px * config.background_share

    large = [
        change
        for change in outcome.changes
        if change.primary_stream is Stream.RASTER and change.bbox.area >= limit
    ]
    if not large:
        return

    for change in large:
        outcome.changes.remove(change)
        outcome.suppress(
            change,
            "filter_background_update",
            "A single region covering more than 15% of the sheet changed, which is a "
            "background or reference drawing being updated rather than an edit.",
        )

    box = large[0].bbox
    for change in large[1:]:
        box = box.union(change.bbox)
    share = box.area / sheet_area_px * 100.0
    outcome.changes.append(
        ChangeRecord(
            kind=ChangeKind.BACKGROUND_UPDATED,
            bbox=box,
            streams=[Stream.RASTER],
            description=(
                f"A background or reference drawing covering about {share:.0f}% of the "
                "sheet was updated. Check it separately rather than change by change."
            ),
            confidence=0.7,
            detail={"collapsed_from": len(large)},
        )
    )
    outcome.record("filter_background_update_collapsed")


# ── The alignment residual detector ─────────────────────────────────────


@dataclass(slots=True)
class ResidualReport:
    """Whether the change map is tracing existing strokes rather than new ones."""

    is_residual: bool = False
    thin_share: float = 0.0
    tracing_share: float = 0.0
    magnitude_px: float = 0.0
    magnitude_paper_mm: float = 0.0
    magnitude_site_mm: float | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_residual": self.is_residual,
            "thin_share": round(self.thin_share, 3),
            "tracing_share": round(self.tracing_share, 3),
            "magnitude_paper_mm": round(self.magnitude_paper_mm, 2),
            "magnitude_site_mm": self.magnitude_site_mm,
            "message": self.message,
        }


def detect_alignment_residual(
    change_mask: NDArray[np.uint8] | None,
    old_binary: NDArray[np.uint8] | None,
    new_binary: NDArray[np.uint8] | None,
    tolerance: ResolvedTolerance,
    config: FilterConfig | None = None,
) -> ResidualReport:
    """Is this change map showing new geometry, or tracing the old geometry?

    A slightly imperfect alignment produces a thin halo along every stroke on
    the sheet. Reported as changes, that is hundreds of fake findings and a
    user who never opens the tool again. The signature is unmistakable once
    you look for it: the change components are **thin**, and their centres
    lie on strokes that exist in *both* images.

    When it is found the right answer is not to report the changes. It is to
    say the sheet did not align well enough, in millimetres, and offer manual
    alignment.
    """
    config = config or FilterConfig()
    report = ResidualReport()
    if change_mask is None or old_binary is None or new_binary is None:
        return report
    if not change_mask.any():
        return report

    binary = (change_mask > 0).astype(np.uint8)
    total = int(binary.sum())
    if total == 0:
        return report

    # Half the distance transform's ridge value is the local half-width, so
    # twice the median over the changed pixels is the typical width.
    distance = cv2.distanceTransform(binary * 255, cv2.DIST_L2, 3)
    widths = distance[binary > 0] * 2.0
    thin = float((widths <= config.residual_max_width_px).sum()) / total
    report.thin_share = thin
    report.magnitude_px = float(np.median(widths)) if widths.size else 0.0
    report.magnitude_paper_mm = tolerance.px_to_paper_mm(report.magnitude_px)
    report.magnitude_site_mm = tolerance.px_to_site_mm(report.magnitude_px)

    # Does the change sit on ink that is present in both sheets? A halo does.
    # New geometry does not, because there was nothing there before.
    shared = cv2.bitwise_and(old_binary, new_binary)
    radius = max(round(config.residual_max_width_px), 2)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    near_shared = cv2.dilate(shared, kernel)
    tracing = float(((binary > 0) & (near_shared > 0)).sum()) / total
    report.tracing_share = tracing

    if thin >= config.residual_share and tracing >= config.residual_share:
        report.is_residual = True
        site = report.magnitude_site_mm
        accuracy = (
            f"{site:.1f} mm on site"
            if site is not None
            else f"{report.magnitude_paper_mm:.2f} mm on paper"
        )
        scale = f" at 1:{tolerance.scale_denominator}" if tolerance.scale_denominator else ""
        report.message = (
            f"This sheet aligned to about {accuracy}, which is not tight enough for "
            f"reliable comparison{scale}. The differences found trace the existing "
            "lines rather than showing new ones, so they are not reported. "
            "Try setting the alignment points manually."
        )
    return report


def apply_residual(outcome: FilterOutcome, report: ResidualReport) -> None:
    """Drop the geometry findings and say why, rather than reporting a halo."""
    if not report.is_residual:
        return
    outcome.warnings.append(
        Warning_(
            code="alignment_residual",
            message=report.message,
            detail=report.as_dict(),
        )
    )
    for change in list(outcome.changes):
        if change.primary_stream is Stream.TEXT:
            continue  # text matching does not care about a sub-millimetre shift
        outcome.changes.remove(change)
        outcome.suppress(
            change,
            "detect_alignment_residual",
            "The change map traces lines that exist on both sheets, so this is "
            "alignment error rather than a design change.",
        )


# ── The suite ───────────────────────────────────────────────────────────


def run_filters(
    changes: list[ChangeRecord],
    *,
    tolerance: ResolvedTolerance,
    sheet_area_px: float,
    old_stroke_px: float = 0.0,
    new_stroke_px: float = 0.0,
    unchanged_text_boxes: list[Bbox] | None = None,
    old_layers: list[LayerInfo] | None = None,
    new_layers: list[LayerInfo] | None = None,
    old_colour: NDArray[np.uint8] | None = None,
    new_colour: NDArray[np.uint8] | None = None,
    old_binary: NDArray[np.uint8] | None = None,
    new_binary: NDArray[np.uint8] | None = None,
    change_mask: NDArray[np.uint8] | None = None,
    optional_content_bbox: Bbox | None = None,
    config: FilterConfig | None = None,
) -> FilterOutcome:
    """Run every filter, in the order where each one helps the next.

    Residual first: if the whole change map is alignment error there is no
    point filtering its contents. Then the style filters, which collapse
    many records into one. Then font substitution, which needs the text
    stream's verdict. Speckle and background last, on what remains.
    """
    config = config or FilterConfig()
    outcome = FilterOutcome(changes=list(changes))

    residual = detect_alignment_residual(change_mask, old_binary, new_binary, tolerance, config)
    apply_residual(outcome, residual)

    # The layer toggle runs before the rest: it absorbs whole findings, and
    # filtering something that is about to be absorbed is wasted work.
    filter_layer_toggle(
        outcome, old_layers or [], new_layers or [], optional_content_bbox, tolerance
    )

    if not residual.is_residual:
        filter_line_weight(outcome, old_stroke_px, new_stroke_px, tolerance, config)
        filter_line_type(outcome, config)
        filter_colour_plot(outcome, old_colour, new_colour, old_binary, new_binary)
        filter_font_substitution(outcome, unchanged_text_boxes or [], tolerance)
        filter_speckle(outcome, tolerance, config)
        filter_background_update(outcome, sheet_area_px, config)

    logger.debug(
        "Noise filters | kept={} | suppressed={} | {}",
        len(outcome.changes),
        len(outcome.filtered),
        outcome.counts,
    )
    return outcome
