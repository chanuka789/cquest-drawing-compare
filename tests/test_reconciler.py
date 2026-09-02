"""Set reconciliation: the core of Phase 2.

The test that matters most is `test_a_partial_issue_reports_no_removals`. If
the app tells a user that 37 drawings were removed when the consultant simply
reissued three, the tool looks broken and never gets opened again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.core.enums import IssueType, RegisterStatus, Side
from engine.core.models import ListEntry, SheetRecord
from engine.core.pipeline import intake_folder
from engine.register.reconciler import reconcile
from engine.register.summary import build_sentence
from tests.fixture_builder import build_normal_pair, build_partial_issue_pair


def sheet(
    number: str | None,
    revision: str | None = "C",
    *,
    side: Side = Side.NEW,
    content: str = "hash-1",
    path: str | None = None,
    title: str | None = None,
    readable: bool = True,
    filename: str | None = None,
) -> SheetRecord:
    """A sheet record, without going near a real PDF."""
    from engine.titleblock.field_extractor import normalise_number

    name = filename or f"{number or 'unnamed'}.pdf"
    return SheetRecord(
        side=side,
        abs_path=path or f"D:\\{side}\\{name}",
        rel_path=name,
        filename=name,
        drawing_no=number,
        normalised_no=normalise_number(number),
        source_of_number="titleblock" if number else "none",
        revision=revision,
        title=title,
        content_hash=content,
        is_readable=readable,
    )


def by_number(result) -> dict[str, object]:
    return {row.drawing_no: row for row in result.rows}


# ── The statuses ───────────────────────────────────────────────────────


def test_a_revised_drawing_is_revised():
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD, content="a")],
        [sheet("A-101", "D", content="b")],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-101"]

    assert row.status is RegisterStatus.REVISED
    assert row.old_revision == "C"
    assert row.new_revision == "D"
    assert row.is_comparable
    assert "Revised from C to D" in row.note


def test_an_identical_file_is_unchanged():
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD, content="same")],
        [sheet("A-101", "C", content="same")],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-101"]

    assert row.status is RegisterStatus.UNCHANGED
    assert not row.is_comparable  # no point comparing it
    assert not row.needs_attention


def test_same_revision_different_file_is_flagged_loudly():
    """Someone reissued without bumping the revision. The money finding."""
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD, content="old-bytes")],
        [sheet("A-101", "C", content="new-bytes")],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-101"]

    assert row.status is RegisterStatus.SAME_REV_DIFFERENT_FILE
    assert row.needs_attention
    assert row.is_comparable  # compare it anyway
    assert "without a revision change" in row.note


def test_a_new_drawing_is_new():
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD)],
        [sheet("A-101", "C"), sheet("A-999", "A")],
        issue_type=IssueType.FULL,
    )
    assert by_number(result)["A-999"].status is RegisterStatus.NEW


def test_a_status_change_is_its_own_status():
    result = reconcile(
        [sheet("A-101", "P03", side=Side.OLD, content="a")],
        [sheet("A-101", "C01", content="b")],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-101"]

    assert row.status is RegisterStatus.STATUS_CHANGE
    assert row.is_comparable
    assert "not a revision decrease" in row.note


def test_an_unreadable_file_is_quarantined_in_the_register():
    result = reconcile(
        [],
        [sheet(None, None, readable=False, filename="locked.pdf")],
        issue_type=IssueType.FULL,
    )
    row = result.rows[0]

    assert row.status is RegisterStatus.UNREADABLE
    assert row.needs_attention


def test_a_sheet_with_no_number_is_unidentified():
    result = reconcile([], [sheet(None, "C", filename="scan001.pdf")], issue_type=IssueType.FULL)
    row = result.rows[0]

    assert row.status is RegisterStatus.UNIDENTIFIED
    assert row.drawing_no == "scan001.pdf"  # show the filename, not a blank
    assert row.needs_attention
    assert "Enter the number" in row.note or "enter the" in row.note.lower()


# ── ⚠ Partial issues ───────────────────────────────────────────────────


def test_an_unknown_issue_type_with_lopsided_sets_asks_rather_than_guessing():
    old = [sheet(f"A-{100 + i}", "C", side=Side.OLD) for i in range(40)]
    new = [sheet("A-100", "D"), sheet("A-101", "D")]

    result = reconcile(old, new, issue_type=IssueType.UNKNOWN)

    assert result.needs_issue_type_confirmation
    assert not result.rows  # no register until the question is answered

    question = result.issue_type_question
    assert question is not None
    assert question.old_count == 40
    assert question.new_count == 2
    assert "40" in question.question and "2" in question.question

    ids = {option["id"] for option in question.options}
    assert {"partial", "full", "compare_reissued"} == ids


def test_a_partial_issue_reports_no_removals():
    """The rule that decides whether people trust the tool."""
    old = [sheet(f"A-{100 + i}", "C", side=Side.OLD) for i in range(40)]
    new = [sheet("A-100", "D", content="x"), sheet("A-101", "D", content="y")]

    result = reconcile(old, new, issue_type=IssueType.PARTIAL)

    statuses = {row.status for row in result.rows}
    assert RegisterStatus.REMOVED not in statuses

    not_reissued = [r for r in result.rows if r.status is RegisterStatus.NOT_REISSUED]
    assert len(not_reissued) == 38
    assert all(not row.needs_attention for row in not_reissued)
    assert "Nothing to do" in not_reissued[0].note


def test_a_full_issue_does_report_removals():
    old = [sheet("A-101", "C", side=Side.OLD), sheet("A-102", "C", side=Side.OLD)]
    new = [sheet("A-101", "C")]

    result = reconcile(old, new, issue_type=IssueType.FULL)
    row = by_number(result)["A-102"]

    assert row.status is RegisterStatus.REMOVED
    assert row.needs_attention
    assert "Confirm whether" in row.note


def test_similar_set_sizes_do_not_trigger_the_question():
    old = [sheet(f"A-{100 + i}", "C", side=Side.OLD) for i in range(10)]
    new = [sheet(f"A-{100 + i}", "C") for i in range(9)]

    result = reconcile(old, new, issue_type=IssueType.UNKNOWN)

    assert not result.needs_issue_type_confirmation
    assert result.rows


# ── Duplicates and supersession ────────────────────────────────────────


def test_two_revisions_in_one_folder_keep_the_highest():
    new = [
        sheet("A-101", "C", content="c", path="D:\\new\\A-101-RevC.pdf"),
        sheet("A-101", "D", content="d", path="D:\\new\\A-101-RevD.pdf"),
    ]
    result = reconcile(
        [sheet("A-101", "B", side=Side.OLD, content="b")], new, issue_type=IssueType.FULL
    )
    row = by_number(result)["A-101"]

    assert row.new_revision == "D"
    assert row.superseded_paths == ["D:\\new\\A-101-RevC.pdf"]


def test_an_identical_file_in_two_places_is_a_duplicate_not_a_supersession():
    new = [
        sheet("A-101", "C", content="same", path="D:\\new\\A-101.pdf"),
        sheet("A-101", "C", content="same", path="D:\\new\\Copy of A-101.pdf"),
    ]
    result = reconcile([], new, issue_type=IssueType.FULL)
    row = by_number(result)["A-101"]

    assert len(row.duplicate_paths) == 1
    assert not row.superseded_paths


# ── Matching ───────────────────────────────────────────────────────────


def test_numbers_match_across_different_separators():
    """`A-101` and `A_101` are the same drawing written two ways."""
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD, content="a")],
        [sheet("A_101", "D", content="b")],
        issue_type=IssueType.FULL,
    )

    assert len(result.rows) == 1
    assert result.rows[0].status is RegisterStatus.REVISED


# ── The drawing list ───────────────────────────────────────────────────


def test_a_drawing_the_list_expects_but_neither_folder_has():
    result = reconcile(
        [sheet("A-101", "C", side=Side.OLD)],
        [sheet("A-101", "C")],
        drawing_list=[
            ListEntry(drawing_no="A-101", normalised_no="A101"),
            ListEntry(drawing_no="A-500", normalised_no="A500", title="MISSING PLAN"),
        ],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-500"]

    assert row.status is RegisterStatus.IN_LIST_NOT_IN_FOLDER
    assert row.needs_attention
    assert "Chase it" in row.note


def test_a_drawing_in_the_folder_but_not_on_the_list():
    result = reconcile(
        [],
        [sheet("A-999", "A")],
        drawing_list=[ListEntry(drawing_no="A-101", normalised_no="A101")],
        issue_type=IssueType.FULL,
    )
    row = by_number(result)["A-999"]

    assert row.status is RegisterStatus.IN_FOLDER_NOT_IN_LIST
    assert "Query it" in row.note


# ── Ordering and summary ───────────────────────────────────────────────


def test_rows_needing_attention_sort_to_the_top():
    result = reconcile(
        [
            sheet("A-101", "C", side=Side.OLD, content="a"),
            sheet("A-102", "C", side=Side.OLD, content="same"),
        ],
        [sheet("A-101", "C", content="b"), sheet("A-102", "C", content="same")],
        issue_type=IssueType.FULL,
    )

    assert result.rows[0].status is RegisterStatus.SAME_REV_DIFFERENT_FILE
    assert result.rows[0].needs_attention


def test_the_summary_counts_and_reads_as_a_sentence():
    old = [sheet(f"A-{100 + i}", "C", side=Side.OLD, content=f"h{i}") for i in range(5)]
    new = [sheet("A-100", "D", content="new"), sheet("A-101", "C", content="h1")]

    result = reconcile(old, new, issue_type=IssueType.PARTIAL)
    summary = result.summary

    assert summary.old_sheet_count == 5
    assert summary.new_sheet_count == 2
    assert summary.counts[RegisterStatus.REVISED] == 1
    assert summary.counts[RegisterStatus.NOT_REISSUED] == 3
    assert "revised" in summary.sentence
    assert "partial issue" in summary.sentence


def test_the_sentence_uses_neutral_words_for_a_partial_issue():
    sentence = build_sentence(
        {RegisterStatus.NOT_REISSUED: 188, RegisterStatus.REVISED: 12}, 0, IssueType.PARTIAL
    )

    assert "188 not reissued" in sentence
    assert "removed" not in sentence
    assert "need no action" in sentence


def test_an_empty_pair_says_so():
    result = reconcile([], [], issue_type=IssueType.FULL)
    assert "No drawings were found" in result.summary.sentence


# ── End to end on generated fixtures ───────────────────────────────────


@pytest.fixture(scope="module")
def normal_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return build_normal_pair(tmp_path_factory.mktemp("normal"))


def test_a_real_folder_pair_reconciles(normal_pair):
    old_dir, new_dir = normal_pair

    old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="t-old")
    new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="t-new")

    result = reconcile(old_sheets, new_sheets, issue_type=IssueType.FULL)
    rows = by_number(result)

    # Three sheets were revised C -> D, one is brand new, the rest unchanged.
    assert rows["A-101"].status is RegisterStatus.REVISED
    assert rows["A-102"].status is RegisterStatus.REVISED
    assert rows["A-201"].status is RegisterStatus.REVISED
    assert rows["A-303"].status is RegisterStatus.NEW
    assert rows["A-103"].status is RegisterStatus.UNCHANGED
    assert result.summary.counts[RegisterStatus.REVISED] == 3


def test_a_generated_partial_issue_reports_no_removals(tmp_path: Path):
    old_dir, new_dir = build_partial_issue_pair(tmp_path / "partial", old_count=20)

    old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="p-old")
    new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="p-new")

    asked = reconcile(old_sheets, new_sheets, issue_type=IssueType.UNKNOWN)
    assert asked.needs_issue_type_confirmation

    answered = reconcile(old_sheets, new_sheets, issue_type=IssueType.PARTIAL)
    statuses = {row.status for row in answered.rows}

    assert RegisterStatus.REMOVED not in statuses
    assert answered.summary.counts[RegisterStatus.NOT_REISSUED] == 17
    assert answered.summary.counts[RegisterStatus.REVISED] == 3
