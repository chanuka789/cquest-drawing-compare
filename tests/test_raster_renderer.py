"""Task 4.1: the render core.

These tests pin the empirical findings documented in
``engine/extract/raster_renderer.py``: BGR buffers become RGB through
``rev_byteorder``, pdfium applies ``/Rotate`` itself (upright output with
rotation 0), crop-based banding cannot match a full render bitwise so the
module renders once and converts in bands, and annotations are isolated by
render difference.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pikepdf
import pytest

from engine.extract.raster_renderer import (
    RenderOptions,
    binarize,
    detect_scanned,
    estimate_ink_coverage,
    render_page,
)
from engine.utils.errors import UnreadableFileError, ValidationError
from engine.utils.pdf_runtime import open_document
from tests.fixture_builder import (
    A1_HEIGHT,
    A1_WIDTH,
    A3_HEIGHT,
    A3_WIDTH,
    SheetSpec,
    build_corrupt_pdf,
    build_pdf,
    build_scanned_pdf,
)


def _explicit_render(path: Path, *, dpi: int, rotation: int, draw_annots: bool = False):
    """Render through pdfium directly, matching the module's own settings."""
    with open_document(path) as doc:
        page = doc[0]
        bitmap = page.render(
            scale=dpi / 72,
            rotation=rotation,
            draw_annots=draw_annots,
            rev_byteorder=True,
        )
        array = np.array(bitmap.to_numpy(), copy=True)
        bitmap.close()
    return array


def _copy_with_rotation(source: Path, target: Path, rotate: int) -> Path:
    with pikepdf.open(source) as pdf:
        pdf.pages[0].Rotate = rotate
        pdf.save(target)
    return target


# ── Channel order and dimensions ───────────────────────────────────────


def test_colour_is_rgb_not_bgr(tmp_path: Path) -> None:
    """The documented empirical finding: renders read RGB, not BGR."""
    path = tmp_path / "colours.pdf"
    pdf = pikepdf.Pdf.new()
    content = pdf.make_stream(b"1 0 0 rg 10 10 140 180 re f\n0 0 1 rg 150 10 140 180 re f\n")
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 300, 200],
        Resources=pikepdf.Dictionary(),
        Contents=content,
    )
    pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))
    pdf.save(path)
    pdf.close()

    result = render_page(path, options=RenderOptions(dpi=100))
    scale = 100 / 72
    assert result.colour is not None
    assert result.colour.shape == (math.ceil(200 * scale), math.ceil(300 * scale), 3)
    red = result.colour[int((200 - 100) * scale), int(70 * scale)]
    blue = result.colour[int((200 - 100) * scale), int(220 * scale)]
    assert red.tolist() == [255, 0, 0]
    assert blue.tolist() == [0, 0, 255]


# ── DPI accuracy ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("width", "height", "dpi"),
    [
        (A1_WIDTH, A1_HEIGHT, 200),
        (A3_WIDTH, A3_HEIGHT, 200),
        (A3_WIDTH, A3_HEIGHT, 300),
    ],
)
def test_dpi_accuracy(tmp_path: Path, width: float, height: float, dpi: int) -> None:
    path = build_pdf(tmp_path / "sheet.pdf", [SheetSpec(width=width, height=height)])
    result = render_page(path, options=RenderOptions(dpi=dpi))

    assert result.dpi == dpi
    assert result.px_per_mm == dpi / 25.4
    assert result.mm_width == pytest.approx(width * 25.4 / 72, rel=1e-12)
    assert result.mm_height == pytest.approx(height * 25.4 / 72, rel=1e-12)
    # ceil(pts * dpi/72) must be within one pixel of round(mm * dpi / 25.4).
    assert abs(result.width_px - round(result.mm_width * dpi / 25.4)) <= 1
    assert abs(result.height_px - round(result.mm_height * dpi / 25.4)) <= 1


# ── Colour / grayscale shapes and ink ──────────────────────────────────


def test_colour_and_grayscale_shapes_and_ink(tmp_path: Path) -> None:
    path = build_pdf(
        tmp_path / "sheet.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT, title="GROUND FLOOR PLAN")],
    )
    result = render_page(path, options=RenderOptions(dpi=150))
    assert result.colour is not None
    assert result.grayscale is not None
    assert result.colour.dtype == np.uint8
    assert result.grayscale.dtype == np.uint8
    assert result.colour.shape == (result.height_px, result.width_px, 3)
    assert result.grayscale.shape == (result.height_px, result.width_px)
    # Page background must survive the render as white.
    assert np.any(np.all(result.colour == 255, axis=2))
    # Border plus text: some ink, but far less than half of the sheet.
    coverage = estimate_ink_coverage(result.grayscale)
    assert 0.0 < coverage < 0.6


def test_ink_coverage_blank_is_zero() -> None:
    assert estimate_ink_coverage(np.full((40, 40), 255, dtype=np.uint8)) == 0.0
    assert estimate_ink_coverage(np.full((40, 40, 3), 255, dtype=np.uint8)) == 0.0


def test_binarize_methods(tmp_path: Path) -> None:
    path = build_pdf(
        tmp_path / "sheet.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT)],
    )
    result = render_page(path, options=RenderOptions(dpi=150))
    assert result.colour is not None
    assert result.grayscale is not None
    for method in ("otsu", "adaptive"):
        binary = binarize(result.grayscale, method=method)
        assert set(np.unique(binary).tolist()) <= {0, 255}
        assert binary.dtype == np.uint8
        # RGB input must give the same result as grayscale input.
        assert np.array_equal(binary, binarize(result.colour, method=method))
    with pytest.raises(ValueError):
        binarize(result.grayscale, method="nonsense")


# ── Rotation normalisation ─────────────────────────────────────────────


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotation_normalised(tmp_path: Path, rotate: int) -> None:
    """A /Rotate-flagged copy renders upright, bitwise like an explicit
    rotation of the base. (An origin-based media box is used because pdfium
    misplaces one text line by a pixel when a 180-flagged page also has an
    offset media box — a pdfium quirk, not this module's concern.)"""
    base = build_pdf(
        tmp_path / "base.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT, title="FIRST FLOOR PLAN")],
        offset_origin=False,
    )
    rotated = _copy_with_rotation(base, tmp_path / f"rot{rotate}.pdf", rotate)
    dpi = 150

    plain = render_page(base, options=RenderOptions(dpi=dpi))
    upright = render_page(rotated, options=RenderOptions(dpi=dpi))

    assert plain.pdf_rotation == 0
    assert upright.pdf_rotation == rotate
    assert plain.applied_rotation == 0
    assert upright.applied_rotation == 0
    assert upright.colour is not None and plain.colour is not None

    # Upright output of a 90/270-flagged page swaps the canvas dimensions.
    if rotate in (90, 270):
        assert (upright.width_px, upright.height_px) == (
            plain.height_px,
            plain.width_px,
        )
    else:
        assert (upright.width_px, upright.height_px) == (
            plain.width_px,
            plain.height_px,
        )

    # pdfium's own /Rotate handling must equal an explicit rotation render
    # of the unrotated page, bitwise (this is what "always upright" means).
    expected = _explicit_render(base, dpi=dpi, rotation=rotate)
    assert np.array_equal(upright.colour, expected)


def test_rotation_normalised_on_offset_origin_box(tmp_path: Path) -> None:
    """The real-world Lami-style box (not at the origin) still normalises:
    dimensions swap and the flag is recorded, whatever pdfium's internal
    rasterisation does with the page."""
    base = build_pdf(
        tmp_path / "base.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT, title="SITE PLAN")],
    )
    rotated = _copy_with_rotation(base, tmp_path / "rot90.pdf", 90)
    dpi = 150

    plain = render_page(base, options=RenderOptions(dpi=dpi))
    upright = render_page(rotated, options=RenderOptions(dpi=dpi))
    assert upright.pdf_rotation == 90
    assert upright.applied_rotation == 0
    assert (upright.width_px, upright.height_px) == (
        plain.height_px,
        plain.width_px,
    )
    assert upright.colour is not None


# ── Annotations ────────────────────────────────────────────────────────


def _add_square_annotation(source: Path, target: Path) -> Path:
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0]
        page.Annots = pdf.make_indirect(
            pikepdf.Array(
                [
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Square,
                        Rect=[100, 200, 900, 700],
                        C=[1, 0, 0],
                        BS=pikepdf.Dictionary(W=4, S=pikepdf.Name.S),
                    )
                ]
            )
        )
        pdf.save(target)
    return target


def test_separate_annotations(tmp_path: Path) -> None:
    # pdfium positions annotation rects relative to the media-box origin, so
    # an offset-origin (Lami-style) page would shift the square; the fixture
    # keeps the box at the origin and the test stays deterministic.
    base = build_pdf(
        tmp_path / "base.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT, title="ROOF PLAN")],
        offset_origin=False,
    )
    annotated_path = _add_square_annotation(base, tmp_path / "annotated.pdf")
    dpi = 150

    content = render_page(annotated_path, options=RenderOptions(dpi=dpi))
    separated = render_page(
        annotated_path,
        options=RenderOptions(dpi=dpi, separate_annotations=True),
    )

    # Content renders are identical whether or not the layer is requested.
    assert content.annotations_mask is None
    assert content.annotations_colour is None
    assert separated.annotations_mask is not None
    assert separated.annotations_colour is not None
    assert np.array_equal(content.colour, separated.colour)
    assert np.array_equal(content.grayscale, separated.grayscale)

    # The mask only covers the annotation rectangle (scaled to pixels).
    scale = dpi / 72
    margin = 12  # stroke width (4 pt -> ~8 px) plus anti-aliasing
    row_min = int((A3_HEIGHT - 700) * scale) - margin
    row_max = int((A3_HEIGHT - 200) * scale) + margin
    col_min = int(100 * scale) - margin
    col_max = int(900 * scale) + margin
    mask = separated.annotations_mask
    assert mask is not None
    rows, cols = np.nonzero(mask)
    assert len(rows) > 0
    assert rows.min() >= row_min and rows.max() <= row_max
    assert cols.min() >= col_min and cols.max() <= col_max

    # Compositing content with the layer where the mask is set reproduces
    # pdfium's with-annotations render exactly.
    assert separated.colour is not None
    assert separated.annotations_colour is not None
    display = separated.colour.copy()
    display[mask > 0] = separated.annotations_colour[mask > 0]
    with_annots = _explicit_render(annotated_path, dpi=dpi, rotation=0, draw_annots=True)
    assert np.array_equal(display, with_annots)


def test_no_annotations_yields_empty_layer(tmp_path: Path) -> None:
    base = build_pdf(
        tmp_path / "base.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT)],
    )
    result = render_page(
        base,
        options=RenderOptions(dpi=100, separate_annotations=True),
    )
    assert result.annotations_mask is not None
    assert result.annotations_colour is not None
    assert not np.any(result.annotations_mask)
    assert not np.any(result.annotations_colour)


# ── Banded memory path ─────────────────────────────────────────────────


def test_banded_equals_full_render_bitwise(tmp_path: Path) -> None:
    """The memory-safety proof: banded output == full output, bit for bit."""
    path = build_pdf(
        tmp_path / "a1.pdf",
        [SheetSpec(width=A1_WIDTH, height=A1_HEIGHT, title="SITE PLAN")],
    )
    dpi = 300
    banded = render_page(path, options=RenderOptions(dpi=dpi, memory_budget_mb=8))
    full = render_page(path, options=RenderOptions(dpi=dpi, memory_budget_mb=4096))

    assert banded.rendered_in_bands is True
    assert full.rendered_in_bands is False
    assert banded.colour is not None
    assert full.colour is not None
    assert banded.grayscale is not None
    assert full.grayscale is not None
    assert np.array_equal(banded.colour, full.colour)
    assert np.array_equal(banded.grayscale, full.grayscale)
    assert (banded.width_px, banded.height_px) == (full.width_px, full.height_px)


def test_banded_chosen_by_estimate(tmp_path: Path) -> None:
    """An 8 MB budget forces the banded path for a 300-dpi A1 sheet."""
    path = build_pdf(tmp_path / "a1.pdf", [SheetSpec(width=A1_WIDTH, height=A1_HEIGHT)])
    result = render_page(path, options=RenderOptions(dpi=300, memory_budget_mb=8))
    assert result.rendered_in_bands is True


# ── Scan detection ─────────────────────────────────────────────────────


def test_detect_scanned(tmp_path: Path) -> None:
    scanned = build_scanned_pdf(tmp_path / "scanned.pdf")
    text = build_pdf(
        tmp_path / "text.pdf",
        [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT)],
    )
    with open_document(scanned) as doc:
        assert detect_scanned(doc[0]) is True
    with open_document(text) as doc:
        assert detect_scanned(doc[0]) is False


# ── Typed failures ─────────────────────────────────────────────────────


def test_render_failures_are_typed(tmp_path: Path) -> None:
    corrupt = build_corrupt_pdf(tmp_path / "corrupt.pdf")
    with pytest.raises(UnreadableFileError):
        render_page(corrupt)

    valid = build_pdf(tmp_path / "one-page.pdf", [SheetSpec(width=A3_WIDTH, height=A3_HEIGHT)])
    with pytest.raises(ValidationError):
        render_page(valid, page_index=2)
    with pytest.raises(ValidationError):
        render_page(valid, options=RenderOptions(dpi=0))
