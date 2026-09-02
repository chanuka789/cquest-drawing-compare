"""Title block reading: zones, labels, drawing numbers, revisions, scales.

The two regression tests that matter most here came from real drawings:

* `test_the_value_under_the_right_label_wins` — the neighbouring "Project No."
  cell sits exactly as far below its label as the drawing number does below
  its own, so a distance-only match read every sheet as `LM2426`.
* `test_a_specific_label_beats_a_generic_one` — a materials legend headed
  "DESCRIPTION" near the top of the sheet was beating the title block's own
  "Drawing Title" cell.
"""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from engine.extract.text_extractor import extract_document_text, extract_page_text
from engine.titleblock.field_extractor import (
    NumberSource,
    extract_identity,
    find_label,
    normalise_number,
    number_from_filename,
)
from engine.titleblock.patterns import (
    SheetProfile,
    find_numbers,
    find_revision,
    is_plausible_number,
    is_plausible_revision,
    load_profile,
)
from engine.titleblock.scale_reader import read_scale, scales_are_equivalent
from engine.titleblock.zone_detector import ZoneName, best_zone, detect_zones
from tests.fixture_builder import SheetSpec, build_pdf

#: The real drawing set, when it is present on this machine.
REAL_FIXTURE = Path(r"D:\GitHub\_cqdc-fixtures\old\LM2426_D_00-Al Basateen Farm_KSA-VILLA.pdf")
needs_real_fixture = pytest.mark.skipif(
    not REAL_FIXTURE.is_file(), reason="the real drawing set is not on this machine"
)


@pytest.fixture(scope="module")
def sheet_page(tmp_path_factory: pytest.TempPathFactory):
    """One synthetic A1 sheet, with a media box that is not at the origin."""
    path = build_pdf(
        tmp_path_factory.mktemp("tb") / "A-101-Rev-C.pdf",
        [SheetSpec(drawing_no="A-101", title="GROUND FLOOR PLAN", revision="C", scale="1 : 100")],
    )
    pages = extract_document_text(str(path))
    return path, pages[0]


# ── Text extraction ────────────────────────────────────────────────────


def test_text_is_extracted_with_positions(sheet_page):
    _, page = sheet_page

    assert not page.is_empty
    assert page.char_count > 20
    assert any("GROUND FLOOR PLAN" in item.clean for item in page.items)


def test_page_box_is_not_assumed_to_start_at_the_origin(sheet_page):
    """The real fixture's box runs from (-1192, -842). Fractions must still work."""
    _, page = sheet_page

    assert page.box.x0 < 0
    assert page.box.y0 < 0
    assert all(0.0 <= item.fx <= 1.0 for item in page.items)
    assert all(0.0 <= item.fy <= 1.0 for item in page.items)


def test_a_page_with_no_text_layer_returns_empty(tmp_path: Path):
    from tests.fixture_builder import build_scanned_pdf

    path = build_scanned_pdf(tmp_path / "scan.pdf")
    document = pdfium.PdfDocument(str(path))
    try:
        page = extract_page_text(document[0], 0)
    finally:
        document.close()

    assert page.is_empty


# ── Zones ──────────────────────────────────────────────────────────────


def test_the_title_block_zone_is_found(sheet_page):
    _, page = sheet_page
    zone = best_zone(page)

    assert zone is not None
    assert zone.name is not ZoneName.WHOLE_SHEET
    assert zone.score > 0
    assert "Drawing No." in zone.text()


def test_the_whole_sheet_is_always_a_last_resort(sheet_page):
    _, page = sheet_page
    zones = detect_zones(page)

    assert zones[-1].name is ZoneName.WHOLE_SHEET


def test_body_text_is_outside_the_title_block_zone(sheet_page):
    """Dimensions in the drawing area must not be mistaken for the number."""
    _, page = sheet_page
    corner = next(zone for zone in detect_zones(page) if zone.name is ZoneName.BOTTOM_RIGHT)

    assert "GRID A" not in corner.text()


# ── Labels and values ──────────────────────────────────────────────────


def test_the_value_under_the_right_label_wins(sheet_page):
    """Regression: 'Project No.' sits the same distance below its own label
    as the drawing number does, so a distance-only match read the project
    number for every sheet in the real set."""
    _, page = sheet_page
    identity = extract_identity(page, filename="A-101-Rev-C.pdf")

    assert identity.drawing_no == "A-101"
    assert identity.drawing_no != "LM2426"  # the project number


def test_a_specific_label_beats_a_generic_one():
    """Regression: 'description' matched a legend header above the title block."""
    from engine.extract.text_extractor import PageBox, PageText, TextItem

    box = PageBox(0, 0, 1000, 1000)
    page = PageText(page_index=0, box=box)
    page.items = [
        # A legend header near the top of the sheet.
        TextItem("DESCRIPTION", 800, 950, 90, 10, 8, 0.80, 0.95),
        TextItem("12mm THK. Gypsum Board", 800, 930, 150, 10, 8, 0.80, 0.93),
        # The real title cell, lower down.
        TextItem("Drawing Title", 800, 200, 70, 10, 8, 0.80, 0.20),
        TextItem("ROOF PLAN", 800, 180, 80, 12, 10, 0.80, 0.18),
    ]

    profile = load_profile()
    label = find_label(page.items, profile.title_labels)

    assert label is not None
    assert label.clean == "Drawing Title"


def test_a_value_to_the_right_of_its_label_is_found(sheet_page):
    """`Scale` puts its value to the right, not below."""
    _, page = sheet_page
    identity = extract_identity(page, filename="A-101-Rev-C.pdf")

    assert identity.scale == "1:100"


def test_title_and_revision_are_read(sheet_page):
    _, page = sheet_page
    identity = extract_identity(page, filename="A-101-Rev-C.pdf")

    assert identity.title == "GROUND FLOOR PLAN"
    assert identity.revision == "C"


# ── Source of the number ───────────────────────────────────────────────


def test_the_source_of_the_number_is_recorded(sheet_page):
    """A QS trusts a title block number and checks a filename one."""
    _, page = sheet_page
    identity = extract_identity(page, filename="A-101-Rev-C.pdf")

    assert identity.source_of_number is NumberSource.TITLEBLOCK
    assert identity.number_confidence >= 0.9
    assert "title block" in identity.source_explanation


def test_a_sheet_without_a_title_block_falls_back_to_the_filename(tmp_path: Path):
    path = build_pdf(
        tmp_path / "A-999-Rev-B.pdf",
        [SheetSpec(include_title_block=False, body=["JUST A PLAN"])],
    )
    page = extract_document_text(str(path))[0]
    identity = extract_identity(page, filename=path.name)

    assert identity.drawing_no == "A-999"
    assert identity.source_of_number is NumberSource.FILENAME
    assert identity.number_confidence < 0.9


def test_a_sheet_with_nothing_to_go_on_is_unidentified(tmp_path: Path):
    path = build_pdf(
        tmp_path / "scan copy.pdf",
        [SheetSpec(include_title_block=False, body=["NO NUMBERS HERE"])],
    )
    page = extract_document_text(str(path))[0]
    identity = extract_identity(page, filename=path.name)

    assert not identity.identified
    assert identity.source_of_number is NumberSource.NONE
    assert identity.warnings
    assert "enter" in identity.warnings[0].lower()  # says what to do


# ── The mismatch flag ──────────────────────────────────────────────────


def test_a_title_block_and_filename_disagreement_is_flagged(sheet_page):
    """Document controllers check this first, so it must be caught."""
    _, page = sheet_page
    identity = extract_identity(page, filename="A-999-wrongly-named.pdf")

    assert identity.drawing_no == "A-101"  # the sheet is believed
    assert identity.filename_number == "A-999"
    assert identity.number_mismatch
    assert "A-101" in identity.warnings[0] and "A-999" in identity.warnings[0]


def test_agreement_is_not_flagged(sheet_page):
    _, page = sheet_page
    identity = extract_identity(page, filename="A-101-Ground-Floor-Plan-RevC.pdf")

    assert not identity.number_mismatch


def test_separators_do_not_count_as_a_mismatch(sheet_page):
    """`A_101` and `A-101` are the same drawing written two ways."""
    _, page = sheet_page
    identity = extract_identity(page, filename="A_101 Rev C.pdf")

    assert not identity.number_mismatch


# ── Filenames and normalising ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("A-101-Rev-C.pdf", "A-101"),
        ("A_101_Rev_C.pdf", "A-101"),
        ("A 101 ground floor.pdf", "A-101"),
        ("UVU-KEO-ARC-L03-DR-A-001234-Rev-D.pdf", "UVU-KEO-ARC-L03-DR-A-001234"),
        ("scan0001.pdf", None),
        ("", None),
    ],
)
def test_number_from_filename(filename, expected):
    assert number_from_filename(filename) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A-101", "A101"), ("a_101", "A101"), ("A 101", "A101"), (None, ""), ("", "")],
)
def test_normalise_number(raw, expected):
    assert normalise_number(raw) == expected


# ── Patterns ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A-101", True),
        ("G-000", True),
        ("UVU-KEO-ARC-L03-DR-A-001234", True),
        ("3000", False),  # a dimension, not a drawing
        ("REV", False),
        ("A1", False),  # a sheet size
        ("", False),
    ],
)
def test_plausible_numbers(text, expected):
    assert is_plausible_number(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("A", True), ("P01", True), ("C02", True), ("01", True), ("URGENT", False), ("", False)],
)
def test_plausible_revisions(text, expected):
    assert is_plausible_revision(text) is expected


def test_find_numbers_prefers_the_more_specific_pattern():
    found = find_numbers("Drawing UVU-KEO-ARC-L03-DR-A-001234 sheet A-101")
    assert found[0] == "UVU-KEO-ARC-L03-DR-A-001234"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("REV C", "C"),
        ("Rev. D", "D"),
        ("REVISION: A", "A"),
        ("P03", "P03"),
        ("nothing here", None),
    ],
)
def test_find_revision(text, expected):
    assert find_revision(text) == expected


def test_a_profile_can_add_its_own_patterns():
    profile = SheetProfile(id="acme", number_patterns=[r"\bACME/\d{4}\b"])
    assert find_numbers("Drawing ACME/1234 issued", profile) == ["ACME/1234"]


def test_the_shipped_profiles_load():
    from engine.titleblock.patterns import available_profiles

    ids = {entry["id"] for entry in available_profiles()}
    assert "default" in ids
    assert "keo" in ids
    assert load_profile("keo").label == "KEO A1"


def test_an_unknown_profile_falls_back_to_the_default():
    assert load_profile("no-such-profile").id == "default"


# ── Scales ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1:100", "1:100"),
        ("1 : 100", "1:100"),
        ("1/50", "1:50"),
        ("NTS", "NTS"),
        ("Not to scale", "NTS"),
        ("As shown", "As shown"),
        ("nothing", None),
    ],
)
def test_read_scale(text, expected):
    assert read_scale(text).text == expected


def test_several_scales_lower_the_confidence():
    """A sheet with details at other scales is worth flagging, not guessing."""
    single = read_scale("Scale 1:100")
    several = read_scale("1:100 and detail at 1:5")

    assert single.confidence > several.confidence
    assert several.text == "1:100"


def test_scale_equivalence():
    assert scales_are_equivalent("1:100", "1 : 100")
    assert scales_are_equivalent("1/50", "1:50")
    assert not scales_are_equivalent("1:100", "1:50")


# ── The real drawing set ───────────────────────────────────────────────


@needs_real_fixture
def test_identification_rate_on_the_real_drawing_set():
    """The plan's target: 90% or more identified from the title block."""
    pages = extract_document_text(str(REAL_FIXTURE))
    identities = [extract_identity(page, filename=REAL_FIXTURE.name) for page in pages.values()]

    from_title_block = [
        item for item in identities if item.source_of_number is NumberSource.TITLEBLOCK
    ]
    rate = len(from_title_block) / len(identities)

    assert rate >= 0.9, f"only {rate:.0%} identified from the title block"


@needs_real_fixture
def test_the_real_drawing_numbers_are_correct():
    """A high rate of *wrong* numbers is worse than a low rate of right ones."""
    pages = extract_document_text(str(REAL_FIXTURE))
    numbers = {
        extract_identity(page, filename=REAL_FIXTURE.name).drawing_no for page in pages.values()
    }

    # These appear in the drawing list on page 1 of the same file.
    assert {"A-101", "A-102", "A-103", "A-201", "G-001"} <= numbers
    assert "LM2426" not in numbers  # the project number, not a drawing number


@needs_real_fixture
def test_the_real_titles_and_scales_are_read():
    pages = extract_document_text(str(REAL_FIXTURE))
    by_number = {
        item.drawing_no: item
        for item in (extract_identity(page, filename=REAL_FIXTURE.name) for page in pages.values())
    }

    assert by_number["A-101"].title == "GROUND FLOOR PLAN"
    assert by_number["A-102"].title == "FIRST FLOOR PLAN"
    assert by_number["A-101"].scale == "1:100"
    assert by_number["A-104"].title == "GROUND FLOOR REFLECTED CEILING PLAN"
