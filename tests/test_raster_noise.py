"""The raster stream and the noise filter suite.

The raster stream is tolerant on purpose: it asks whether there is ink
*nearby*, not whether there is ink exactly here. The filters are the product —
each one is a specific, common false positive with a specific fix, and every
one of them says in words what it suppressed.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from engine.compare.noise_filter import (
    FilterConfig,
    FilterOutcome,
    detect_alignment_residual,
    filter_background_update,
    filter_colour_plot,
    filter_font_substitution,
    filter_layer_toggle,
    filter_line_type,
    filter_line_weight,
    filter_speckle,
    run_filters,
)
from engine.compare.raster_diff import (
    RasterDiffConfig,
    binarise,
    diff_raster,
    estimate_stroke_width,
    looks_scanned,
    tolerant_difference,
)
from engine.compare.tolerance import ToleranceSpec, resolve_for_sheet
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    Stream,
    VectorChangeDetail,
)
from engine.extract.vector_extractor import LayerInfo

TOLERANCE = resolve_for_sheet(ToleranceSpec(dpi=200), scale_text="1:100", dpi=200)
SIZE = (600, 800)


def blank() -> np.ndarray:
    return np.full(SIZE, 255, dtype=np.uint8)


def sheet_with_lines(thickness: int = 2, offset: int = 0) -> np.ndarray:
    image = blank()
    for y in (100, 200, 300, 400):
        cv2.line(image, (50, y + offset), (750, y + offset), 0, thickness)
    return image


# ── The raster stream ───────────────────────────────────────────────────


def test_identical_sheets_report_nothing():
    image = sheet_with_lines()
    result = diff_raster(image, image.copy(), None, TOLERANCE)

    assert result.regions == []
    assert result.changes == []


def test_a_heavier_pen_is_absorbed_by_the_tolerance():
    """A one-pixel line weight change must not light up the whole sheet."""
    thin = sheet_with_lines(thickness=2)
    thick = sheet_with_lines(thickness=4)

    result = diff_raster(thin, thick, None, TOLERANCE)

    assert result.changes == []


def test_a_real_addition_is_found():
    old = sheet_with_lines()
    new = sheet_with_lines()
    cv2.rectangle(new, (400, 450), (600, 550), 0, 3)

    result = diff_raster(old, new, None, TOLERANCE)

    assert result.changes
    assert any(change.bbox.intersects(Bbox(400, 450, 200, 100)) for change in result.changes)


def test_tolerant_matching_is_not_an_exclusive_or():
    old = blank()
    new = blank()
    cv2.line(old, (100, 100), (500, 100), 0, 2)
    cv2.line(new, (100, 101), (500, 101), 0, 2)  # shifted one pixel

    old_binary = binarise(old, RasterDiffConfig())
    new_binary = binarise(new, RasterDiffConfig())
    removed, added = tolerant_difference(old_binary, new_binary, tolerance_px=3.0)

    assert removed.sum() == 0
    assert added.sum() == 0


def test_stroke_width_is_estimated_from_the_ink():
    thin = binarise(sheet_with_lines(thickness=2), RasterDiffConfig())
    thick = binarise(sheet_with_lines(thickness=6), RasterDiffConfig())

    assert estimate_stroke_width(thick) > estimate_stroke_width(thin)


def test_a_clean_plot_is_not_mistaken_for_a_scan():
    assert not looks_scanned(sheet_with_lines())


def test_an_unevenly_lit_scan_is_recognised():
    image = sheet_with_lines().astype(np.int16)
    gradient = np.linspace(0, -90, SIZE[1], dtype=np.int16)[None, :]
    scanned = np.clip(image + gradient, 0, 255).astype(np.uint8)

    assert looks_scanned(scanned)


# ── The filters ─────────────────────────────────────────────────────────


def style_change(width_old: float = 1.0, width_new: float = 3.0) -> ChangeRecord:
    return ChangeRecord(
        kind=ChangeKind.STYLE_ONLY,
        bbox=Bbox(0, 0, 100, 10),
        streams=[Stream.VECTOR],
        is_cosmetic=True,
        vector=VectorChangeDetail(
            style_difference=f"line weight {width_old:.2f} mm → {width_new:.2f} mm"
        ),
    )


def raster_change(box: Bbox, area: float | None = None) -> ChangeRecord:
    return ChangeRecord(
        kind=ChangeKind.REMOVED,
        bbox=box,
        streams=[Stream.RASTER],
        detail={"pixel_area": area if area is not None else box.area},
    )


def test_many_line_weight_records_collapse_into_one():
    outcome = FilterOutcome(changes=[style_change() for _ in range(200)])
    filter_line_weight(outcome, 2.0, 5.0, TOLERANCE)

    assert len(outcome.changes) == 1
    assert outcome.changes[0].is_cosmetic
    assert "200 objects" in outcome.changes[0].description
    assert len(outcome.filtered) == 200


def test_the_line_weight_collapse_does_not_wait_for_the_raster_estimate():
    """The vector stream can prove a pen change on its own."""
    outcome = FilterOutcome(changes=[style_change() for _ in range(50)])
    filter_line_weight(outcome, 0.0, 0.0, TOLERANCE)  # no raster measurement

    assert len(outcome.changes) == 1
    assert "replotted with a different pen weight" in outcome.changes[0].description


def test_line_type_records_collapse_too():
    changes = [
        ChangeRecord(
            kind=ChangeKind.STYLE_ONLY,
            bbox=Bbox(0, 0, 10, 10),
            streams=[Stream.VECTOR],
            is_cosmetic=True,
            vector=VectorChangeDetail(style_difference="line type"),
        )
        for _ in range(30)
    ]
    outcome = FilterOutcome(changes=changes)
    filter_line_type(outcome)

    assert len(outcome.changes) == 1
    assert "30 objects differ only in line type" in outcome.changes[0].description


def test_font_substitution_suppresses_raster_noise_inside_unchanged_text():
    text_box = Bbox(100, 100, 200, 20)
    outcome = FilterOutcome(changes=[raster_change(Bbox(150, 104, 30, 12))])
    filter_font_substitution(outcome, [text_box], TOLERANCE)

    assert outcome.changes == []
    assert "the font, not the wording" in outcome.filtered[0].reason


def test_font_substitution_leaves_changes_outside_the_text_alone():
    outcome = FilterOutcome(changes=[raster_change(Bbox(900, 900, 40, 40))])
    filter_font_substitution(outcome, [Bbox(100, 100, 200, 20)], TOLERANCE)

    assert len(outcome.changes) == 1


def test_a_colour_plot_against_a_mono_one_is_cosmetic():
    colour = np.zeros((100, 100, 3), dtype=np.uint8)
    colour[:, :, 2] = 200  # saturated blue
    mono = np.full((100, 100, 3), 60, dtype=np.uint8)  # grey
    binary = np.zeros((100, 100), dtype=np.uint8)
    binary[40:60, 40:60] = 255

    outcome = FilterOutcome(changes=[raster_change(Bbox(0, 0, 50, 50))])
    filter_colour_plot(outcome, colour, mono, binary, binary.copy())

    assert outcome.changes == []
    assert any(warning.code == "plot_settings_changed" for warning in outcome.warnings)


def test_a_toggled_layer_is_one_record_that_absorbs_its_own_content():
    region = Bbox(100, 100, 400, 400)
    outcome = FilterOutcome(
        changes=[raster_change(Bbox(120 + index * 20, 120, 10, 300)) for index in range(15)]
    )
    filter_layer_toggle(
        outcome,
        [LayerInfo("SETTING OUT", visible=True)],
        [LayerInfo("SETTING OUT", visible=False)],
        region,
        TOLERANCE,
    )

    assert len(outcome.changes) == 1
    change = outcome.changes[0]
    assert change.kind is ChangeKind.LAYER_VISIBILITY_CHANGED
    assert change.is_cosmetic
    assert "SETTING OUT" in change.description
    assert len(outcome.filtered) == 15


def test_an_unchanged_layer_produces_no_record():
    outcome = FilterOutcome()
    filter_layer_toggle(
        outcome, [LayerInfo("GRID", visible=True)], [LayerInfo("GRID", visible=True)]
    )
    assert outcome.changes == []


def test_speckle_is_removed_and_the_reason_kept():
    outcome = FilterOutcome(changes=[raster_change(Bbox(500, 500, 2, 2), area=2)])
    filter_speckle(outcome, TOLERANCE)

    assert outcome.changes == []
    assert outcome.filtered[0].filter_name == "filter_speckle"
    assert "Too small" in outcome.filtered[0].reason


def test_a_real_region_survives_the_speckle_filter():
    outcome = FilterOutcome(changes=[raster_change(Bbox(500, 500, 80, 80))])
    filter_speckle(outcome, TOLERANCE)

    assert len(outcome.changes) == 1


def test_a_background_update_collapses_into_one_record():
    sheet_area = 1000.0 * 1000.0
    outcome = FilterOutcome(
        changes=[raster_change(Bbox(0, 0, 600, 600)), raster_change(Bbox(100, 100, 700, 500))]
    )
    filter_background_update(outcome, sheet_area)

    assert len(outcome.changes) == 1
    assert outcome.changes[0].kind is ChangeKind.BACKGROUND_UPDATED
    assert "background or reference drawing" in outcome.changes[0].description


# ── The alignment residual detector ─────────────────────────────────────


def test_a_halo_tracing_existing_strokes_is_residual():
    """The signature of a slightly imperfect alignment: thin, and on the ink."""
    old = blank()
    new = blank()
    for y in range(50, 550, 25):
        cv2.line(old, (50, y), (750, y), 0, 3)
        cv2.line(new, (50, y + 1), (750, y + 1), 0, 3)

    old_binary = binarise(old, RasterDiffConfig())
    new_binary = binarise(new, RasterDiffConfig())
    removed, added = tolerant_difference(old_binary, new_binary, tolerance_px=0.5)
    change_mask = cv2.bitwise_or(removed, added)

    report = detect_alignment_residual(change_mask, old_binary, new_binary, TOLERANCE)

    assert report.is_residual
    assert report.thin_share > 0.7
    assert "not tight enough" in report.message
    assert "manually" in report.message


def test_a_genuine_change_is_not_residual():
    old = sheet_with_lines()
    new = sheet_with_lines()
    cv2.rectangle(new, (300, 450), (600, 560), 0, -1)  # a solid block: new ink

    old_binary = binarise(old, RasterDiffConfig())
    new_binary = binarise(new, RasterDiffConfig())
    removed, added = tolerant_difference(old_binary, new_binary, tolerance_px=2.0)
    change_mask = cv2.bitwise_or(removed, added)

    report = detect_alignment_residual(change_mask, old_binary, new_binary, TOLERANCE)

    assert not report.is_residual


def test_residual_hides_geometry_findings_but_never_the_text_ones():
    """A sub-millimetre shift cannot damage a string comparison."""
    old = blank()
    new = blank()
    for y in range(50, 550, 25):
        cv2.line(old, (50, y), (750, y), 0, 3)
        cv2.line(new, (50, y + 1), (750, y + 1), 0, 3)
    old_binary = binarise(old, RasterDiffConfig())
    new_binary = binarise(new, RasterDiffConfig())
    removed, added = tolerant_difference(old_binary, new_binary, tolerance_px=0.5)

    text_change = ChangeRecord(
        kind=ChangeKind.MODIFIED, bbox=Bbox(10, 10, 40, 12), streams=[Stream.TEXT]
    )
    outcome = run_filters(
        [text_change, raster_change(Bbox(100, 100, 200, 3))],
        tolerance=TOLERANCE,
        sheet_area_px=float(SIZE[0] * SIZE[1]),
        old_binary=old_binary,
        new_binary=new_binary,
        change_mask=cv2.bitwise_or(removed, added),
    )

    assert outcome.changes == [text_change]
    assert any(warning.code == "alignment_residual" for warning in outcome.warnings)


def test_every_filter_records_what_it_removed():
    outcome = run_filters(
        [raster_change(Bbox(1, 1, 2, 2), area=2)],
        tolerance=TOLERANCE,
        sheet_area_px=float(SIZE[0] * SIZE[1]),
        config=FilterConfig(),
    )

    assert outcome.changes == []
    assert outcome.filtered
    assert outcome.filtered[0].reason
    assert outcome.counts["filter_speckle"] == 1


def test_nothing_is_ever_discarded_without_a_reason():
    outcome = run_filters(
        [raster_change(Bbox(1, 1, 2, 2), area=2), style_change()],
        tolerance=TOLERANCE,
        sheet_area_px=float(SIZE[0] * SIZE[1]),
    )
    for entry in outcome.filtered:
        assert entry.filter_name
        assert entry.reason
        assert entry.change is not None


@pytest.mark.parametrize("dpi", [150, 200, 300])
def test_the_comparison_dpi_is_never_hard_coded(dpi):
    tolerance = resolve_for_sheet(ToleranceSpec(dpi=dpi), scale_text="1:100", dpi=dpi)
    image = sheet_with_lines()
    result = diff_raster(image, image.copy(), None, tolerance)

    assert result.changes == []
