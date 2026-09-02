"""Known-answer regression tests.

A fixed pair of folders must always produce exactly the same register. These
tests are deliberately written as full expected tables rather than spot
checks: the point is that a future change to matching, revision logic or
reconciliation cannot quietly alter an answer a user has already relied on.

If one of these fails, the question is not "how do I make the test pass" but
"is the new answer actually more correct than the old one". If it is, update
the expectation in the same commit as the change, and say why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.core.enums import IssueType, RegisterStatus, Side
from engine.core.pipeline import intake_folder
from engine.register.reconciler import reconcile
from tests.fixture_builder import (
    SheetSpec,
    build_corrupt_pdf,
    build_normal_pair,
    build_partial_issue_pair,
    build_pdf,
)

#: The exact register for the `normal` fixture pair, as a full issue.
GOLDEN_NORMAL: dict[str, tuple[str, str | None, str | None]] = {
    # drawing number: (status, old revision, new revision)
    "A-001": ("unchanged", "C", "C"),
    "A-101": ("revised", "C", "D"),
    "A-102": ("revised", "C", "D"),
    "A-103": ("unchanged", "C", "C"),
    "A-104": ("unchanged", "C", "C"),
    "A-105": ("unchanged", "C", "C"),
    "A-201": ("revised", "C", "D"),
    "A-202": ("unchanged", "C", "C"),
    "A-301": ("unchanged", "C", "C"),
    "A-302": ("unchanged", "C", "C"),
    "A-303": ("new", None, "A"),
}


@pytest.fixture(scope="module")
def golden_register(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("golden")
    old_dir, new_dir = build_normal_pair(root)

    old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="golden-old")
    new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="golden-new")

    return reconcile(old_sheets, new_sheets, issue_type=IssueType.FULL)


def test_the_register_matches_the_known_answer_exactly(golden_register):
    actual = {
        row.drawing_no: (str(row.status), row.old_revision, row.new_revision)
        for row in golden_register.rows
    }

    assert actual == GOLDEN_NORMAL


def test_the_counts_match_the_known_answer(golden_register):
    assert golden_register.summary.counts == {
        RegisterStatus.REVISED: 3,
        RegisterStatus.UNCHANGED: 7,
        RegisterStatus.NEW: 1,
    }
    assert golden_register.summary.old_sheet_count == 10
    assert golden_register.summary.new_sheet_count == 11
    assert golden_register.summary.attention_count == 0


def test_every_number_comes_from_the_title_block(golden_register):
    """A drop here means title block extraction has regressed."""
    sources = {row.source_of_number for row in golden_register.rows}
    assert sources == {"titleblock"}


def test_the_summary_sentence_reads_correctly(golden_register):
    sentence = golden_register.summary.sentence
    assert sentence.startswith("3 revised, 1 new, 7 unchanged.")
    assert "removed" not in sentence
    assert "attention" not in sentence


# ── The partial issue, which is the one that matters ───────────────────


def test_a_partial_issue_has_a_known_answer_with_no_removals(tmp_path: Path):
    old_dir, new_dir = build_partial_issue_pair(tmp_path / "golden-partial", old_count=12)

    old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="gp-old")
    new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="gp-new")

    result = reconcile(old_sheets, new_sheets, issue_type=IssueType.PARTIAL)
    actual = {row.drawing_no: str(row.status) for row in result.rows}

    expected = {f"A-{100 + index}": "not_reissued" for index in range(12)}
    for index in range(3):
        expected[f"A-{100 + index}"] = "revised"

    assert actual == expected
    assert RegisterStatus.REMOVED not in result.summary.counts


def test_the_same_input_gives_the_same_answer_twice(tmp_path: Path):
    """Reconciliation must be repeatable: an audit trail depends on it."""
    old_dir, new_dir = build_normal_pair(tmp_path / "repeat")

    def run() -> list[tuple[str, str]]:
        old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="r-old")
        new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="r-new")
        result = reconcile(old_sheets, new_sheets, issue_type=IssueType.FULL)
        return [(row.drawing_no, str(row.status)) for row in result.rows]

    assert run() == run()


def test_an_unreadable_file_does_not_change_the_other_rows(tmp_path: Path):
    """One bad file must not disturb the register for everything else."""
    old_dir, new_dir = build_normal_pair(tmp_path / "with-bad")

    old_sheets, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="b-old")
    new_sheets, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="b-new")
    clean = reconcile(old_sheets, new_sheets, issue_type=IssueType.FULL)

    build_corrupt_pdf(new_dir / "damaged.pdf")
    build_pdf(
        new_dir / "mystery.pdf",
        [SheetSpec(include_title_block=False, body=["NO NUMBER HERE"])],
    )

    old_again, _ = intake_folder(str(old_dir), side=Side.OLD, run_id="b2-old")
    new_again, _ = intake_folder(str(new_dir), side=Side.NEW, run_id="b2-new")
    dirty = reconcile(old_again, new_again, issue_type=IssueType.FULL)

    good_before = {row.drawing_no: str(row.status) for row in clean.rows}
    good_after = {
        row.drawing_no: str(row.status)
        for row in dirty.rows
        if row.status not in {RegisterStatus.UNREADABLE, RegisterStatus.UNIDENTIFIED}
    }

    assert good_after == good_before
    assert dirty.summary.counts[RegisterStatus.UNREADABLE] == 1
    assert dirty.summary.counts[RegisterStatus.UNIDENTIFIED] == 1
