"""Masking: what is never compared, and what must never be masked.

Get this wrong in one direction and every sheet reports the revision letter
and the date as changes. Get it wrong in the other and a changed general
note — often the most expensive change on the sheet — disappears silently.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from engine.extract.raster_renderer import RenderOptions, render_page
from engine.extract.text_extractor import extract_document_text
from engine.masking.mask_engine import (
    MaskSet,
    apply_mask_to_image,
    apply_mask_to_items,
    build_mask,
    invert_mask,
    list_profiles,
    load_from_profile,
    mask_array,
    save_to_profile,
)
from engine.masking.template_detect import (
    apply_template,
    cluster_sheets,
    detect_template_zones,
    fingerprint_frame,
)
from engine.masking.titleblock_zones import detect_protected_regions, detect_zones, find_long_lines
from engine.masking.types import FracRect, ProtectedType, SheetView, Zone, ZoneType
from engine.masking.watermark_detect import (
    detect_annotations,
    detect_stamps,
    detect_watermarks,
    detections_to_zones,
)
from engine.utils.errors import NotFoundError, ValidationError
from tests.fixture_builder import write_drawing_pdf
from tests.harness.inject import (
    AddWatermark,
    ChangeSpec,
    inject_changes,
)

DPI = 150


def view(path: str | Path, dpi: int = DPI) -> SheetView:
    page = extract_document_text(str(path))[0]
    render = render_page(str(path), 0, RenderOptions(dpi=dpi, colour=False))
    return SheetView(page_text=page, gray=render.grayscale, dpi=dpi, source_path=str(path))


@pytest.fixture(scope="module")
def sheet(tmp_path_factory: pytest.TempPathFactory) -> SheetView:
    root = tmp_path_factory.mktemp("masking")
    path = write_drawing_pdf(
        root / "a-101.pdf",
        drawing_no="A-101",
        revision="C",
        scale="1 : 100",
        labels=["RM-01", "3000", "D-12", "NORTH", "GENERAL NOTES", "W3"],
    )
    return view(path)


@pytest.fixture(scope="module")
def watermarked(tmp_path_factory: pytest.TempPathFactory) -> SheetView:
    root = tmp_path_factory.mktemp("masking-wm")
    source = write_drawing_pdf(root / "source.pdf", labels=["RM-01", "3000", "D-12"])
    pair = inject_changes(
        source, 0, ChangeSpec(name="wm", new=[AddWatermark("PRELIMINARY")]), out_dir=root
    )
    return view(pair.new_path)


# ── Geometry of the frame ───────────────────────────────────────────────


def test_long_lines_are_found_as_page_fractions(sheet: SheetView):
    lines = find_long_lines(sheet.gray)

    assert lines.found
    assert any(value < 0.05 for value in lines.verticals)  # the left border
    assert any(value > 0.95 for value in lines.verticals)  # the right border
    assert all(0.0 <= value <= 1.0 for value in lines.horizontals)


def test_a_sheet_with_no_image_still_detects_from_labels(sheet: SheetView):
    text_only = SheetView(page_text=sheet.page_text, dpi=sheet.dpi)
    zones = detect_zones(text_only)

    assert zones.titleblock is not None


# ── The title block ─────────────────────────────────────────────────────


def test_the_title_block_is_found_with_its_evidence(sheet: SheetView):
    zones = detect_zones(sheet)
    block = zones.titleblock

    assert block is not None
    assert block.confidence > 0.7
    assert block.evidence
    # It lives in the bottom-right corner of this sheet.
    assert block.rect.x1 > 0.9
    assert block.rect.y1 > 0.9


def test_the_title_block_does_not_swallow_the_whole_bottom_strip(sheet: SheetView):
    """Snapping to the only long vertical lines on the sheet — its own border —
    would mask the scale bar and anything else along the bottom."""
    block = detect_zones(sheet).titleblock
    assert block is not None
    assert block.rect.width < 0.5


def test_the_frame_is_masked_as_bands_not_as_the_page(sheet: SheetView):
    """Masking the rectangle the border encloses masks the entire drawing."""
    zones = detect_zones(sheet)
    frame_zones = zones.of_type(ZoneType.FRAME)

    assert frame_zones
    assert all(zone.rect.area < 0.1 for zone in frame_zones)


def test_title_block_text_is_excluded_and_drawing_text_is_kept(sheet: SheetView):
    zones = detect_zones(sheet)
    mask = build_mask(sheet)
    mask.zones.extend(zones.zones)
    mask.protected.extend(zones.protected)

    kept, dropped = apply_mask_to_items(sheet.page_text.items, mask, sheet)
    kept_text = {item.clean for item in kept}
    dropped_text = {item.clean for item in dropped}

    assert "A-101" in dropped_text
    assert "C" in dropped_text  # the revision letter: the whole point
    assert "1 : 100" in dropped_text
    assert {"RM-01", "3000", "D-12"} <= kept_text


def test_protected_regions_are_detected_and_never_masked(sheet: SheetView):
    protected = detect_protected_regions(sheet)
    kinds = {region.type for region in protected}

    assert ProtectedType.GENERAL_NOTES in kinds
    assert ProtectedType.NORTH_ARROW in kinds


def test_protection_beats_masking():
    mask = MaskSet()
    mask.add_user_rect(FracRect(0.0, 0.0, 1.0, 1.0), "everything")
    mask.protect(FracRect(0.4, 0.4, 0.6, 0.6), "general notes")

    assert mask.covers(0.1, 0.1)
    assert not mask.covers(0.5, 0.5)


# ── Watermarks ──────────────────────────────────────────────────────────


def test_a_rotated_watermark_is_detected_with_its_signals(watermarked: SheetView):
    found = detect_watermarks(watermarked)

    assert found
    detection = found[0]
    assert "PRELIMINARY" in detection.text.upper()
    assert detection.confidence > 0.7
    assert any("degrees" in signal for signal in detection.signals)
    assert any("watermark phrase" in signal for signal in detection.signals)


def test_a_watermark_is_excluded_by_ink_not_by_its_box(watermarked: SheetView):
    """Its box covers a third of the sheet; masking that hides the drawing."""
    zones = detections_to_zones(detect_watermarks(watermarked))
    watermark = next(zone for zone in zones if zone.type is ZoneType.WATERMARK)

    assert watermark.ink_only
    assert watermark.rect.area > 0.1  # genuinely large
    # A drawing label under the watermark is still compared.
    assert not watermark.contains(*watermark.rect.centre)
    assert watermark.suppresses_text(*watermark.rect.centre, "PRELIMINARY")
    assert not watermark.suppresses_text(*watermark.rect.centre, "W3")


def test_a_drawing_title_is_not_mistaken_for_a_watermark(tmp_path: Path):
    """Large and wide, but square to the sheet, solid black and not a phrase."""
    path = write_drawing_pdf(
        tmp_path / "big-title.pdf",
        labels=["GROUND FLOOR GENERAL ARRANGEMENT PLAN"],
        label_size=48.0,
    )
    found = detect_watermarks(view(path))

    assert found == []


def test_stamps_and_annotations_are_detected(tmp_path: Path):
    path = write_drawing_pdf(
        tmp_path / "signed.pdf", labels=["RM-01"], content_lines=["Digitally signed by A Smith"]
    )
    stamps = detect_stamps(view(path))

    assert any(stamp.kind == "signature_block" for stamp in stamps)


def test_annotations_come_from_the_render_split():
    mask_layer = np.zeros((200, 200), dtype=np.uint8)
    mask_layer[50:90, 50:120] = 255
    found = detect_annotations(SheetView(width_px=200, height_px=200), mask_layer)

    assert len(found) == 1
    assert found[0].kind == "annotation"
    assert found[0].confidence > 0.9


# ── The mask engine ─────────────────────────────────────────────────────


def test_a_mask_blanks_what_it_covers_and_keeps_the_rest():
    image = np.zeros((100, 100), dtype=np.uint8)
    mask = MaskSet()
    mask.add_user_rect(FracRect(0.0, 0.0, 0.5, 1.0))

    masked = apply_mask_to_image(image, mask)

    assert masked[:, :49].mean() == 255
    assert masked[:, 51:].mean() == 0


def test_an_ink_only_zone_removes_pale_ink_and_keeps_the_drawing():
    image = np.full((100, 100), 255, dtype=np.uint8)
    image[10:20, :] = 170  # a pale watermark stroke
    image[50:60, :] = 20  # a solid drawing line
    mask = MaskSet()
    mask.zones.append(
        Zone(
            type=ZoneType.WATERMARK,
            rect=FracRect(0.0, 0.0, 1.0, 1.0),
            ink_only=True,
            ink_threshold=130,
            match_text="PRELIMINARY",
        )
    )

    masked = apply_mask_to_image(image, mask)

    assert masked[10:20, :].mean() == 255  # the watermark is gone
    assert masked[50:60, :].mean() == pytest.approx(20)  # the drawing stays


def test_a_polygon_zone_masks_its_own_shape():
    mask = MaskSet()
    mask.add_user_polygon([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    covered = mask_array(mask, 100, 100)

    assert covered[5, 5]  # inside the triangle
    assert not covered[95, 95]  # outside it


def test_a_polygon_needs_three_points():
    with pytest.raises(ValidationError):
        MaskSet().add_user_polygon([(0.0, 0.0), (1.0, 1.0)])


def test_the_debug_view_shows_only_what_was_excluded():
    image = np.zeros((100, 100), dtype=np.uint8)
    mask = MaskSet()
    mask.add_user_rect(FracRect(0.0, 0.0, 0.5, 1.0))

    inverted = invert_mask(mask, image)

    assert inverted[:, :49].mean() == 0
    assert inverted[:, 51:].mean() == 255


def test_a_disabled_zone_keeps_its_evidence():
    mask = MaskSet()
    zone = mask.add_user_rect(FracRect(0.1, 0.1, 0.2, 0.2), "title block")
    mask.set_enabled(0, False)

    assert not mask.covers(0.15, 0.15)
    assert zone.evidence  # not deleted, just switched off
    assert mask.zones[0].user_edited


def test_a_mask_round_trips_through_the_profile(tmp_path: Path):
    mask = MaskSet(name="house-style", template_id="template-abcd1234")
    mask.add_user_rect(FracRect(0.7, 0.85, 1.0, 1.0), "title block")
    mask.zones.append(
        Zone(
            type=ZoneType.WATERMARK,
            rect=FracRect(0.1, 0.1, 0.9, 0.9),
            ink_only=True,
            ink_threshold=140,
            match_text="PRELIMINARY",
        )
    )
    mask.protect(FracRect(0.0, 0.0, 0.2, 0.2), "north arrow")

    save_to_profile(mask, profile_id="house-style", directory=tmp_path)
    loaded = load_from_profile("house-style", directory=tmp_path)

    assert loaded.template_id == "template-abcd1234"
    assert len(loaded.zones) == 2
    assert loaded.zones[1].ink_only
    assert loaded.zones[1].match_text == "PRELIMINARY"
    assert loaded.protected[0].label == "north arrow"
    assert list_profiles(tmp_path) == ["house-style"]


def test_a_missing_profile_is_a_clear_error(tmp_path: Path):
    with pytest.raises(NotFoundError):
        load_from_profile("nothing-here", directory=tmp_path)


# ── Templates ───────────────────────────────────────────────────────────


def test_sheets_on_the_same_frame_cluster_together(tmp_path: Path):
    sheets = []
    for index in range(4):
        path = write_drawing_pdf(
            tmp_path / f"a-{index}.pdf",
            drawing_no=f"A-10{index}",
            labels=[f"RM-{index}", "3000"],
        )
        sheets.append(view(path, dpi=100))

    templates = cluster_sheets(sheets)

    assert len(templates) == 1
    assert templates[0].sheet_count == 4
    assert templates[0].representative in templates[0].members


def test_a_portrait_sheet_is_a_different_template(tmp_path: Path):
    landscape = view(write_drawing_pdf(tmp_path / "land.pdf", labels=["RM-01"]), dpi=100)
    portrait = view(
        write_drawing_pdf(tmp_path / "port.pdf", labels=["RM-01"], width=1684.0, height=2384.0),
        dpi=100,
    )

    templates = cluster_sheets([landscape, portrait])

    assert len(templates) == 2


def test_a_fingerprint_is_stable_for_the_same_frame(tmp_path: Path):
    first = view(write_drawing_pdf(tmp_path / "one.pdf", labels=["RM-01"]), dpi=100)
    second = view(write_drawing_pdf(tmp_path / "two.pdf", labels=["RM-99"]), dpi=100)

    assert fingerprint_frame(first).digest == fingerprint_frame(second).digest


def test_zones_detected_once_apply_to_the_whole_cluster(tmp_path: Path):
    sheets = [
        view(write_drawing_pdf(tmp_path / f"s-{index}.pdf", labels=["RM-01"]), dpi=100)
        for index in range(3)
    ]
    template = cluster_sheets(sheets)[0]
    detect_template_zones(template, sheets[template.representative])

    zones, protected = apply_template(template, sheets[1])

    assert zones
    assert len(zones) == len(template.zones)
    assert len(protected) == len(template.protected)
