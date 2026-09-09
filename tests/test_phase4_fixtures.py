"""The Phase 4 alignment fixtures prove every alignment case.

`10_alignment` is built on demand into a tmp dir from
:mod:`tests.fixture_builder` — like the Phase 3 golden fixtures, so the
expectations are pinned by assertions here rather than by files outside the
repository. Each case must behave the way the later alignment engine will
rely on it behaving:

* every pair is readable on both sides, with a text layer where a vector
  sheet should have one and none where it is scanned
* shifted / rotated / rescaled content really moved on the sheet, measured
  from the renders by cv2 phase correlation
* a rotated page flag alone renders upright and identical to the unrotated
  sheet
* the ``impossible`` pair shares no label words: no alignment deserves to
  pass the quality gate
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import cv2
import numpy as np
import pytest

from engine.ingest.pdf_inspector import inspect_pdf
from engine.utils.pdf_runtime import open_document
from tests.fixture_builder import (
    ALIGN_CONTENT_REGION,
    ALIGN_LABEL_COLUMNS,
    ALIGN_LABEL_COUNT,
    ALIGN_LABEL_ROWS,
    ALIGN_TITLEBLOCK_ZONE,
    ALIGNMENT_CASES,
    align_label_fractions,
    align_label_names,
)

RENDER_DPI = 100
#: A millimetre on paper, in rendered pixels.
MM_PX = RENDER_DPI / 25.4
#: The default content band and title-block zone must never overlap, or a
#: title block exclusion would eat real content labels.
assert ALIGN_CONTENT_REGION[1] > ALIGN_TITLEBLOCK_ZONE[3]
#: A 6 x 4 label matrix must hold the full default label count.
assert ALIGN_LABEL_COLUMNS * ALIGN_LABEL_ROWS >= ALIGN_LABEL_COUNT


def _pdfs(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.pdf"))


def _build_case(tmp_path: Path, name: str) -> Path:
    for case_name, builder in ALIGNMENT_CASES:
        if case_name == name:
            builder(tmp_path / name)
            return tmp_path / name
    raise AssertionError(f"no builder for {name}")


def _render_gray(pdf: Path, dpi: float = RENDER_DPI) -> np.ndarray:
    """Render page 0 of *pdf* to grayscale uint8 with the pdfium lock."""
    with open_document(pdf) as document:
        pixels = document[0].render(scale=dpi / 72.0).to_numpy()
    if pixels.ndim == 2:
        return pixels
    if pixels.shape[2] == 3:
        return cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(pixels, cv2.COLOR_BGRA2GRAY)


def _text_items(pdf: Path) -> list:
    """Page-0 text items of *pdf* (an empty list means no text layer)."""
    from engine.extract.text_extractor import extract_document_text

    pages = extract_document_text(pdf)
    return list(pages[0].items) if pages else []


def _outside_title_block(items: list) -> list:
    """Items whose origin sits outside the bottom-right title-block zone."""
    x0, y0, x1, y1 = ALIGN_TITLEBLOCK_ZONE
    return [item for item in items if not (x0 <= item.fx <= x1 and y0 <= item.fy <= y1)]


def _normalised_words(items: list) -> set[str]:
    """NFKC, uppercase word tokens of length >= 2, outside the title block."""
    words: set[str] = set()
    for item in _outside_title_block(items):
        normal = unicodedata.normalize("NFKC", item.text).upper()
        for token in re.split(r"[^A-Z0-9]+", normal):
            if len(token) >= 2:
                words.add(token)
    return words


def _correlate(old: np.ndarray, new: np.ndarray) -> tuple[float, float, float]:
    """Windowed phase correlation: the (dx, dy, response) shift old -> new."""
    height, width = old.shape
    assert new.shape == old.shape
    window = cv2.createHanningWindow((width, height), cv2.CV_64F).astype(np.float32)
    (dx, dy), response = cv2.phaseCorrelate(
        old.astype(np.float32) * window, new.astype(np.float32) * window
    )
    return float(dx), float(dy), float(response)


def _best_rotation(old: np.ndarray, new: np.ndarray) -> tuple[int, float, float, float]:
    """Which 90-degree rotation of *old* best matches *new*?

    Returns (rotation index 0..3, response, dx, dy) for the winner.
    """
    height, width = old.shape
    best: tuple[int, float, float, float] = (-1, -1.0, 0.0, 0.0)
    for k in range(4):
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 90 * k, 1.0)
        candidate = cv2.warpAffine(old, matrix, (width, height), borderValue=255)
        dx, dy, response = _correlate(candidate, new)
        if response > best[1]:
            best = (k, response, dx, dy)
    return best


#: Expected text behaviour per case: (old has text, new has text, max chars).
#: The scanned case's old side is a normal vector sheet; its new side is a
#: raster with no text at all. `sparse_text` keeps its text deliberately low.
CASE_TEXT_EXPECTATIONS: dict[str, tuple[bool, bool, int | None]] = {
    "clean_pair": (True, True, None),
    "shifted": (True, True, None),
    "rescaled": (True, True, None),
    "rotated": (True, True, None),
    "page_rotated": (True, True, None),
    "scanned": (True, False, None),
    "no_grid": (True, True, None),
    "sparse_text": (True, True, 90),
    "impossible": (True, True, None),
}


@pytest.mark.parametrize(
    ("case_name", "builder"),
    [(name, builder) for name, builder in ALIGNMENT_CASES],
    ids=[name for name, _ in ALIGNMENT_CASES],
)
def test_each_case_is_a_readable_pair(tmp_path, case_name, builder):
    """Every `10_alignment` case has old/ and new/ with readable PDFs.

    Vector sheets carry a text layer; the scanned re-issue must not. The
    sparse_text sheet carries text, but deliberately little of it.
    """
    case_dir = tmp_path / case_name
    builder(case_dir)

    old_pdfs = _pdfs(case_dir / "old")
    new_pdfs = _pdfs(case_dir / "new")
    assert old_pdfs, f"{case_name}: no PDFs under old/"
    assert new_pdfs, f"{case_name}: no PDFs under new/"

    old_has_text, new_has_text, max_chars = CASE_TEXT_EXPECTATIONS[case_name]

    def check_side(pdf: Path, expected_text: bool) -> None:
        info = inspect_pdf(pdf)
        assert info.is_readable, f"{case_name}: {pdf.name} unreadable: {info.error_note}"
        assert info.page_count == 1
        page = info.pages[0]
        assert page.has_text is expected_text, (
            f"{case_name}: {pdf.name} text_chars={page.text_chars}"
        )
        if max_chars is not None:
            assert page.text_chars <= max_chars, f"{case_name}: {pdf.name} not sparse"
        if case_name == "scanned" and pdf.parent.name == "new":
            assert page.image_count > 0
            assert page.looks_scanned

    for pdf in old_pdfs:
        check_side(pdf, old_has_text)
    for pdf in new_pdfs:
        check_side(pdf, new_has_text)


def test_clean_pair_changes_exactly_one_label(tmp_path):
    """clean_pair: RM-07 becomes RM-70 and nothing else moves.

    Every other label keeps its text and its position, which is what lets a
    text-anchor alignment pair this as an identity transform with one small
    real change.
    """
    case_dir = _build_case(tmp_path, "clean_pair")
    old_items = _outside_title_block(_text_items(case_dir / "old" / "A-101-RevC.pdf"))
    new_items = _outside_title_block(_text_items(case_dir / "new" / "A-101-RevD.pdf"))

    old_labels = {item.clean for item in old_items}
    new_labels = {item.clean for item in new_items}
    expected = set(align_label_names())
    assert old_labels == expected
    assert new_labels == (expected - {"RM-07"}) | {"RM-70"}
    assert "RM-70" not in old_labels

    # Each label occurs exactly once on the sheet, at its declared fraction;
    # the changed label keeps its slot (RM-70 sits where RM-07 was).
    for index in range(ALIGN_LABEL_COUNT):
        name = f"RM-{index + 1:02d}"
        fx, fy = align_label_fractions(index, ALIGN_CONTENT_REGION)
        old_hits = [item for item in old_items if item.clean == name]
        assert len(old_hits) == 1, (name, len(old_hits))
        assert abs(old_hits[0].fx - fx) <= 0.02, (name, old_hits[0].fx, fx)
        assert abs(old_hits[0].fy - fy) <= 0.02, (name, old_hits[0].fy, fy)
        if name == "RM-07":
            hits = [item for item in new_items if item.clean == "RM-70"]
        else:
            hits = [item for item in new_items if item.clean == name]
        assert len(hits) == 1, (name, len(hits))
        assert abs(hits[0].fx - fx) <= 0.02, (name, hits[0].fx, fx)
        assert abs(hits[0].fy - fy) <= 0.02, (name, hits[0].fy, fy)

    # The rendered content sits on top of itself: identity transform.
    old_render = _render_gray(case_dir / "old" / "A-101-RevC.pdf")
    new_render = _render_gray(case_dir / "new" / "A-101-RevD.pdf")
    dx, dy, response = _correlate(old_render, new_render)
    assert response > 0.8
    assert abs(dx) < 8 and abs(dy) < 8


def test_shifted_content_moved_40mm_right(tmp_path):
    """shifted: phase correlation must measure ~40 mm of x movement."""
    case_dir = _build_case(tmp_path, "shifted")
    old_render = _render_gray(case_dir / "old" / "A-101-RevC.pdf")
    new_render = _render_gray(case_dir / "new" / "A-101-RevC-shifted.pdf")

    dx, dy, response = _correlate(old_render, new_render)
    expected = 40.0 * MM_PX
    assert response > 0.4
    assert abs(dx) > 0.5 * expected, "content did not move in x"
    assert abs(abs(dx) - expected) < 25, (dx, expected)
    assert abs(dy) < 30, dy


def test_rotated_content_turned_90_degrees(tmp_path):
    """rotated: the render matches its old sheet at 90 degrees, not at 0."""
    case_dir = _build_case(tmp_path, "rotated")
    old_render = _render_gray(case_dir / "old" / "A-101-RevC.pdf")
    new_render = _render_gray(case_dir / "new" / "A-101-RevC-rotated90.pdf")

    k, response, dx, dy = _best_rotation(old_render, new_render)
    assert k in (1, 3), f"best rotation was {k} * 90 degrees, not 90"
    assert response > 0.5, response
    assert abs(dx) < 30 and abs(dy) < 30
    # The unrotated comparison must be clearly worse: the content moved.
    _, _, flat_response = _correlate(old_render, new_render)
    assert response > 2 * flat_response


def test_rescaled_content_doubled_about_centre(tmp_path):
    """rescaled: the old centre band enlarged 2.0 equals the new sheet band.

    The old sheet draws its content in (0.3..0.7) of each axis; the new sheet
    is the same content scaled 2.0 about the page centre on the same size
    media box, so it occupies (0.1..0.9). Doubling the old band and comparing
    it against the new band must give the identity shift.
    """
    case_dir = _build_case(tmp_path, "rescaled")
    old_pdf = case_dir / "old" / "A-101-RevC-1-100.pdf"
    new_pdf = case_dir / "new" / "A-101-RevD-1-50.pdf"
    old_info = inspect_pdf(old_pdf)
    new_info = inspect_pdf(new_pdf)
    assert old_info.pages[0].width_mm == pytest.approx(new_info.pages[0].width_mm, abs=1.0)
    assert old_info.pages[0].height_mm == pytest.approx(new_info.pages[0].height_mm, abs=1.0)

    old_render = _render_gray(old_pdf)
    new_render = _render_gray(new_pdf)
    height, width = old_render.shape
    band = (slice(int(0.3 * height), int(0.7 * height)), slice(int(0.3 * width), int(0.7 * width)))
    old_band = old_render[band]
    new_height, new_width = new_render.shape
    new_band = new_render[
        int(0.1 * new_height) : int(0.9 * new_height), int(0.1 * new_width) : int(0.9 * new_width)
    ]
    doubled = cv2.resize(
        old_band, (new_band.shape[1], new_band.shape[0]), interpolation=cv2.INTER_AREA
    )
    dx, dy, response = _correlate(doubled, new_band)
    assert response > 0.7, response
    assert abs(dx) < 25 and abs(dy) < 25

    # The new sheet re-labelled its scale from 1 : 100 to 1 : 50.
    new_text = " ".join(item.clean for item in _text_items(new_pdf))
    assert "1 : 50" in new_text


def test_page_rotated_renders_upright_equal(tmp_path):
    """page_rotated: only the /Rotate flag differs, so upright renders match.

    pdfium applies the page rotation, so the two bitmaps differ only by a
    90-degree rotation of the canvas; undoing it must reproduce the old
    render almost exactly.
    """
    case_dir = _build_case(tmp_path, "page_rotated")
    old_render = _render_gray(case_dir / "old" / "A-101-RevC.pdf")
    new_render = _render_gray(case_dir / "new" / "A-101-RevC-rotateflag90.pdf")

    assert old_render.shape != new_render.shape
    assert tuple(sorted(old_render.shape)) == tuple(sorted(new_render.shape))

    differences: list[float] = []
    for k in range(4):
        candidate = np.rot90(old_render, k)
        if candidate.shape != new_render.shape:
            continue
        diff = np.abs(candidate.astype(np.int16) - new_render.astype(np.int16))
        differences.append(float(diff.mean()))
    assert differences, "no rotation matched the bitmap shape"
    assert min(differences) < 2.0, differences


def test_no_grid_pair_renders_identical(tmp_path):
    """no_grid: byte-identical sides render byte-identical images."""
    case_dir = _build_case(tmp_path, "no_grid")
    old_pdf = case_dir / "old" / "A-101-RevC.pdf"
    new_pdf = case_dir / "new" / "A-101-RevC.pdf"
    assert old_pdf.read_bytes() == new_pdf.read_bytes()
    assert np.array_equal(_render_gray(old_pdf), _render_gray(new_pdf))


def test_scanned_reissue_has_no_text_and_one_image(tmp_path):
    """scanned: the new side is a skewed raster with no text layer.

    The old vector sheet stays readable and text-carrying; the scanned
    re-issue holds exactly one full-page image, no text, and renders real
    ink (it is not a blank photocopy).
    """
    case_dir = _build_case(tmp_path, "scanned")
    old_pdf = case_dir / "old" / "A-101-RevC.pdf"
    new_pdf = case_dir / "new" / "A-101-RevC-scanned.pdf"

    old_page = inspect_pdf(old_pdf).pages[0]
    assert old_page.has_text
    new_info = inspect_pdf(new_pdf)
    assert not new_info.has_text_layer
    assert new_info.pages[0].image_count > 0
    assert not _text_items(new_pdf)

    new_render = _render_gray(new_pdf)
    assert 0.005 < (new_render < 200).mean() < 0.5


def test_sparse_text_pair_keeps_three_tiny_labels(tmp_path):
    """sparse_text: exactly the title block plus K1, K2, K3 outside it."""
    case_dir = _build_case(tmp_path, "sparse_text")
    for pdf in (case_dir / "old" / "A-101-RevC.pdf", case_dir / "new" / "A-101-RevD.pdf"):
        labels = {item.clean for item in _outside_title_block(_text_items(pdf))}
        assert labels == {"K1", "K2", "K3"}


def test_impossible_pair_has_disjoint_label_words(tmp_path):
    """impossible: no label word is shared between two different drawings.

    After the title-block corner zone is excluded, the RM set and the SEC-A
    set must not intersect: text-anchor matching finds zero correspondences,
    and no other method deserves to pass the quality gate either.
    """
    case_dir = _build_case(tmp_path, "impossible")
    old_words = _normalised_words(_text_items(case_dir / "old" / "A-101-RevC.pdf"))
    new_words = _normalised_words(_text_items(case_dir / "new" / "S-101-RevA.pdf"))
    assert old_words and new_words

    shared = old_words & new_words
    long_shared = {word for word in shared if len(word) >= 3}
    assert not long_shared, f"shared words of length >= 3: {sorted(long_shared)}"
    assert len(shared) <= 2, f"shared words: {sorted(shared)}"
