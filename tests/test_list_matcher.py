"""Using the drawing list to identify sheets the title block could not.

Priority 4 of the drawing-number order: it fills gaps, and never overrules a
number the sheet gave for itself.
"""

from __future__ import annotations

from engine.core.enums import Side
from engine.core.models import ListEntry, SheetRecord
from engine.register.list_matcher import apply_drawing_list
from engine.titleblock.field_extractor import NumberSource, normalise_number


def entry(number: str, title: str | None = None, revision: str | None = None) -> ListEntry:
    return ListEntry(
        drawing_no=number,
        normalised_no=normalise_number(number),
        title=title,
        revision=revision,
    )


def unidentified(filename: str) -> SheetRecord:
    return SheetRecord(
        side=Side.NEW,
        abs_path=f"D:\\new\\{filename}",
        rel_path=filename,
        filename=filename,
        source_of_number="none",
    )


def identified(number: str, filename: str = "sheet.pdf") -> SheetRecord:
    return SheetRecord(
        side=Side.NEW,
        abs_path=f"D:\\new\\{filename}",
        rel_path=filename,
        filename=filename,
        drawing_no=number,
        normalised_no=normalise_number(number),
        source_of_number="titleblock",
        number_confidence=0.95,
    )


# ── Matching by number ─────────────────────────────────────────────────


def test_a_number_in_the_file_name_is_matched_against_the_list():
    sheets = [unidentified("A-101 ground floor.pdf")]
    result = apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN", "C")])

    assert result.by_number == 1
    assert sheets[0].drawing_no == "A-101"
    assert sheets[0].source_of_number == NumberSource.DRAWING_LIST


def test_the_title_and_revision_come_across_too():
    sheets = [unidentified("A-101.pdf")]
    apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN", "C")])

    assert sheets[0].title == "GROUND FLOOR PLAN"
    assert sheets[0].revision == "C"


def test_the_longest_matching_number_wins():
    """`A-1012` must not be read as `A-101` because that is on the list too."""
    sheets = [unidentified("A-1012.pdf")]
    apply_drawing_list(sheets, [entry("A-101"), entry("A-1012")])

    assert sheets[0].drawing_no == "A-1012"


# ── Matching by title ──────────────────────────────────────────────────


def test_a_file_named_like_a_title_is_matched():
    sheets = [unidentified("Ground Floor Plan.pdf")]
    result = apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN")])

    assert result.by_title == 1
    assert sheets[0].drawing_no == "A-101"


def test_a_vague_file_name_is_left_alone():
    """A wrong number here silently pairs two unrelated drawings."""
    sheets = [unidentified("scan0007.pdf")]
    result = apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN")])

    assert result.total == 0
    assert not sheets[0].identified


def test_a_short_file_name_is_not_fuzzy_matched():
    sheets = [unidentified("plan.pdf")]
    apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN")])

    assert not sheets[0].identified


# ── What it must never do ──────────────────────────────────────────────


def test_a_title_block_number_is_never_overwritten():
    """The sheet is the primary record; the register can be out of date."""
    sheets = [identified("A-999", filename="A-101 ground floor.pdf")]
    result = apply_drawing_list(sheets, [entry("A-101", "GROUND FLOOR PLAN")])

    assert result.total == 0
    assert sheets[0].drawing_no == "A-999"
    assert sheets[0].source_of_number == "titleblock"


def test_an_unreadable_sheet_is_not_given_a_number():
    sheet = unidentified("A-101.pdf")
    sheet.is_readable = False

    result = apply_drawing_list([sheet], [entry("A-101")])

    assert result.total == 0
    assert not sheet.identified


def test_an_empty_list_changes_nothing():
    sheets = [unidentified("A-101.pdf")]
    assert apply_drawing_list(sheets, []).total == 0
    assert not sheets[0].identified


# ── Telling the user ───────────────────────────────────────────────────


def test_the_sheet_records_that_the_number_did_not_come_from_the_drawing():
    sheets = [unidentified("A-101.pdf")]
    apply_drawing_list(sheets, [entry("A-101")])

    warning = sheets[0].warnings[0]
    assert "drawing list, not from the sheet" in warning
    assert "confirm it" in warning
    assert sheets[0].number_confidence < 0.95  # lower than a title block read


def test_the_summary_says_what_happened():
    sheets = [unidentified("A-101.pdf"), unidentified("First Floor Plan.pdf")]
    result = apply_drawing_list(sheets, [entry("A-101"), entry("A-102", "FIRST FLOOR PLAN")])

    assert result.total == 2
    assert "identified 2 more sheets" in result.summary()
    assert "by drawing number" in result.summary()
    assert "by title" in result.summary()


def test_the_summary_is_honest_when_nothing_matched():
    assert "did not identify any" in apply_drawing_list([], []).summary()
