"""Importing a drawing list that a human formatted for printing."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.register.list_importer import import_drawing_list, parse_named_sheet
from engine.register.list_parser import (
    apply_mapping,
    find_header_row,
    guess_mapping,
    is_section_heading,
    normalise_entries,
)
from engine.utils.errors import UnreadableFileError, ValidationError
from tests.fixture_builder import (
    build_clean_drawing_list,
    build_messy_drawing_list,
    build_pdf,
    build_revision_matrix_list,
)


@pytest.fixture(scope="module")
def messy_list(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_messy_drawing_list(tmp_path_factory.mktemp("lists") / "register_messy.xlsx")


# ── The messy file ─────────────────────────────────────────────────────


def test_the_right_worksheet_is_chosen(messy_list: Path):
    """A workbook always has decoy sheets. Pick the one with drawing numbers."""
    result = import_drawing_list(messy_list)

    assert result.sheet_name == "Drawing Register"
    assert "Notes" in result.sheet_names
    assert len(result.sheet_names) == 2


def test_the_header_row_is_found_under_the_logo_block(messy_list: Path):
    """Real registers put the header on row 7, under a project block."""
    result = import_drawing_list(messy_list)
    assert result.header_row == 7


def test_columns_are_mapped_from_their_synonyms(messy_list: Path):
    result = import_drawing_list(messy_list)

    assert result.mapping["drawing_no"] == "Dwg No."
    assert result.mapping["title"] == "Sheet Name"
    assert result.mapping["revision"] == "Rev."
    assert result.mapping["status"] == "Status"


def test_the_preview_is_readable(messy_list: Path):
    result = import_drawing_list(messy_list)

    assert result.ok
    assert result.row_count == 10
    assert len(result.preview) <= 15

    first = result.preview[0]
    assert first.drawing_no == "A-001"
    assert first.title == "SITE PLAN"
    assert first.revision == "C"


def test_section_headings_and_blank_rows_are_skipped(messy_list: Path):
    result = import_drawing_list(messy_list)
    numbers = [row.drawing_no for row in result.rows]

    assert "ARCHITECTURAL" not in numbers
    assert "STRUCTURAL" not in numbers
    assert all(numbers)
    assert any("discipline heading" in warning for warning in result.warnings)


def test_several_worksheets_are_mentioned(messy_list: Path):
    result = import_drawing_list(messy_list)
    assert any("2 worksheets" in warning for warning in result.warnings)


# ── The tidy file ──────────────────────────────────────────────────────


def test_a_clean_list_parses(tmp_path: Path):
    result = import_drawing_list(build_clean_drawing_list(tmp_path / "clean.xlsx"))

    assert result.header_row == 1
    assert result.row_count == 10
    assert result.confidence >= 0.9


# ── Revision matrix ────────────────────────────────────────────────────


def test_a_revision_matrix_is_detected_and_the_latest_issue_used(tmp_path: Path):
    """Some registers have no Rev column: one column per issue date instead."""
    result = import_drawing_list(build_revision_matrix_list(tmp_path / "matrix.xlsx"))

    assert result.is_revision_matrix
    assert len(result.revision_columns) == 3

    by_number = {row.drawing_no: row.revision for row in result.rows}
    assert by_number["A-101"] == "C"  # latest column filled
    assert by_number["A-102"] == "B"  # latest column blank, so the one before
    assert by_number["A-103"] == "A"

    assert any("revision matrix" in warning for warning in result.warnings)


# ── Correcting the mapping ─────────────────────────────────────────────


def test_the_user_can_correct_the_column_mapping(messy_list: Path):
    """Manual mapping with a preview beats clever parsing that is wrong."""
    from engine.register.list_importer import read_workbook_grids

    result = import_drawing_list(messy_list)
    grid = read_workbook_grids(messy_list)["Drawing Register"]

    corrected = apply_mapping(
        result, grid, {"drawing_no": "Dwg No.", "title": "Status", "revision": "Rev."}
    )

    assert corrected.row_count == result.row_count
    assert corrected.preview[0].title == "For construction"  # the column they chose


def test_a_named_worksheet_can_be_re_read(messy_list: Path):
    result = parse_named_sheet(messy_list, "Notes")

    assert result.sheet_name == "Notes"
    assert not result.ok  # there are no drawings on that sheet


def test_asking_for_a_worksheet_that_does_not_exist_says_which_exist(messy_list: Path):
    with pytest.raises(ValidationError) as info:
        parse_named_sheet(messy_list, "No Such Sheet")

    assert "Drawing Register" in info.value.detail["available"]


# ── Other formats ──────────────────────────────────────────────────────


def test_a_csv_list_parses(tmp_path: Path):
    target = tmp_path / "list.csv"
    target.write_text(
        "Drawing No,Title,Rev\nA-101,GROUND FLOOR PLAN,C\nA-102,FIRST FLOOR PLAN,D\n",
        encoding="utf-8",
    )
    result = import_drawing_list(target)

    assert result.row_count == 2
    assert result.rows[0].drawing_no == "A-101"


def test_a_pdf_list_is_read_but_flagged_as_less_certain(tmp_path: Path):
    """A PDF has no real cells, so the table is a reconstruction."""
    from tests.fixture_builder import SheetSpec

    spec = SheetSpec(
        include_title_block=False,
        body=[
            "Drawing No  Title  Rev",
            "A-101  GROUND FLOOR PLAN  C",
            "A-102  FIRST FLOOR PLAN  D",
        ],
    )
    result = import_drawing_list(build_pdf(tmp_path / "list.pdf", [spec]))

    assert result.confidence <= 0.5
    assert any("no real table cells" in warning for warning in result.warnings)


# ── Refusals ───────────────────────────────────────────────────────────


def test_an_unsupported_file_type_says_what_to_use(tmp_path: Path):
    target = tmp_path / "list.docx"
    target.write_bytes(b"not really a document")

    with pytest.raises(ValidationError) as info:
        import_drawing_list(target)

    assert "Excel" in info.value.message


def test_a_missing_file_says_what_may_have_happened(tmp_path: Path):
    with pytest.raises(UnreadableFileError) as info:
        import_drawing_list(tmp_path / "gone.xlsx")

    assert "network drive" in info.value.message


def test_a_damaged_spreadsheet_says_what_to_do(tmp_path: Path):
    target = tmp_path / "broken.xlsx"
    target.write_bytes(b"PK\x03\x04 this is not a real workbook")

    with pytest.raises(UnreadableFileError) as info:
        import_drawing_list(target)

    assert "re-saving it" in info.value.message


# ── Units ──────────────────────────────────────────────────────────────


def test_find_header_row_skips_a_project_block():
    rows = [
        ["ACME ARCHITECTS"],
        [],
        ["Project:", "A tower"],
        ["Drawing No", "Title", "Rev"],
        ["A-101", "PLAN", "C"],
    ]
    assert find_header_row(rows) == 3


def test_find_header_row_returns_none_when_there_is_no_header():
    assert find_header_row([["a", "b"], ["c", "d"]]) is None


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        (["Drawing No", "Title", "Rev"], "Drawing No"),
        (["Dwg No.", "Sheet Name"], "Dwg No."),
        (["Document Number", "Description"], "Document Number"),
        (["Sheet Ref", "Name"], "Sheet Ref"),
    ],
)
def test_guess_mapping_finds_the_number_column(headings, expected):
    assert guess_mapping(headings)["drawing_no"] == expected


def test_a_lone_discipline_word_is_a_section_heading():
    assert is_section_heading({"a": "ARCHITECTURAL", "b": "", "c": ""})
    assert not is_section_heading({"a": "A-101", "b": "PLAN", "c": "C"})
    assert not is_section_heading({"a": "", "b": "", "c": ""})


def test_entries_convert_for_the_reconciler(messy_list: Path):
    entries = normalise_entries(import_drawing_list(messy_list))

    assert len(entries) == 10
    assert entries[0].drawing_no == "A-001"
    assert entries[0].normalised_no == "A001"
