"""The Phase 3 Definition-of-Done, pinned as tests.

Each test maps to a line of Part D of Plans/PHASE-3.md, run over the synthetic
fixtures so the checks hold on every machine without the real drawing set.
"""

from __future__ import annotations

import pytest

from engine.core.models import SheetRecord
from engine.naming.fingerprint import build_document_fingerprints
from engine.naming.matcher import match_sets
from tests.fixture_builder import (
    RENAMED_NEW_ONLY,
    RENAMED_OLD_ONLY,
    RENAMED_SET,
    _renamed_new_filename,
    _renamed_old_filename,
    build_collision_folder,
    build_naming_mess,
    build_renamed_pair,
)


def _load_sheets(folder, side: str) -> list[SheetRecord]:
    sheets: list[SheetRecord] = []
    for pdf in sorted(folder.glob("*.pdf")):
        sheets.append(
            SheetRecord(
                side=side,
                abs_path=str(pdf),
                filename=pdf.name,
                page_count=1,
                drawing_no=None,  # force the name and fingerprint tiers
                is_readable=True,
            )
        )
    return sheets


def _resolver_for(sheets: list[SheetRecord]):
    cache: dict[str, dict[int, object]] = {}

    def resolve(sheet: SheetRecord) -> object | None:
        if sheet.abs_path not in cache:
            cache[sheet.abs_path] = build_document_fingerprints(sheet.abs_path)
        return cache[sheet.abs_path].get(sheet.page_index)

    return resolve


def test_renamed_set_pairs_at_over_95_percent_with_zero_wrong(tmp_path):
    """Part D: 03_renamed pairs >95% correctly with zero wrong pairs."""
    old_dir, new_dir = build_renamed_pair(tmp_path / "renamed")
    old_sheets = _load_sheets(old_dir, "old")
    new_sheets = _load_sheets(new_dir, "new")

    result = match_sets(
        old_sheets, new_sheets, fingerprint_for=_resolver_for([*old_sheets, *new_sheets])
    )

    expected = {
        _renamed_old_filename(old_no, title): _renamed_new_filename(new_no)
        for old_no, new_no, title, _body in RENAMED_SET
    }
    by_old = {pair.old.filename: pair.new.filename for pair in result.pairs}

    assert len(result.pairs) == len(RENAMED_SET)  # coverage: 100% of shared
    for old_file, new_file in expected.items():
        assert by_old[old_file] == new_file, f"wrong pair for {old_file}"
    # and therefore zero wrong pairs, plus honest unmatched lists:
    assert by_old.keys() == set(expected)
    old_only_file = _renamed_old_filename(RENAMED_OLD_ONLY[0], RENAMED_OLD_ONLY[1])
    new_only_file = _renamed_new_filename(RENAMED_NEW_ONLY[0])
    assert old_only_file in {sheet.filename for sheet in result.old_unmatched}
    assert new_only_file in {sheet.filename for sheet in result.new_unmatched}
    assert len(result.old_unmatched) == 1
    assert len(result.new_unmatched) == 1


def test_greedy_vs_optimal_is_proven_by_a_unit_test():
    """Part D: the matcher uses optimal assignment, proven where greedy fails."""
    from tests.test_matcher import test_greedy_would_fail_and_optimal_assignment_wins

    test_greedy_would_fail_and_optimal_assignment_wins()


def test_naming_mess_clean_name_golden(tmp_path):
    """Part D + 3.9: 08_naming_mess normalises to one expected key."""
    from engine.naming.normaliser import NormLevel, normalise

    folder = build_naming_mess(tmp_path / "mess")
    keys = {normalise(pdf.name, NormLevel.AGGRESSIVE).value for pdf in folder.glob("*.pdf")}
    assert keys == {"uvuarc001"}


def test_collision_fixture_names_collide_under_medium(tmp_path):
    """The 09_collision fixture really is a collision at the planner's level."""
    from engine.naming.normaliser import NormLevel, normalise

    folder = build_collision_folder(tmp_path / "collision")
    keys = [normalise(pdf.name, NormLevel.MEDIUM).value for pdf in folder.glob("*.pdf")]
    assert len(set(keys)) == 1


def test_copy_mode_never_touches_input_folders(tmp_path):
    """Part D: copy mode provably never writes into the input folders."""
    from engine.core.session import get_session, reset_session
    from engine.core.workspace import create_workspace

    rename_planner = pytest.importorskip("engine.naming.rename_planner")

    old_dir, new_dir = build_renamed_pair(tmp_path / "issue")
    before_old = {pdf.name for pdf in old_dir.glob("*.pdf")}
    before_new = {pdf.name for pdf in new_dir.glob("*.pdf")}
    new_file_count = len(before_new)

    reset_session()
    session = get_session()
    session.old.folder = str(old_dir)
    session.new.folder = str(new_dir)
    session.new.sheets = _load_sheets(new_dir, "new")
    workspace = create_workspace(tmp_path / "output")

    session.plan_renames("{original}_CLEAN", mode="copy")
    from engine.naming.rename_executor import execute_plan

    outcome = execute_plan(
        session.rename_plan,
        workspace.audit_dir / "rename_log.json",
        mode="copy",
        input_folders=[str(old_dir), str(new_dir)],
    )
    assert not outcome.failed
    assert before_old == {pdf.name for pdf in old_dir.glob("*.pdf")}
    assert before_new == {pdf.name for pdf in new_dir.glob("*.pdf")}
    copied = list(workspace.renamed_dir.glob("*.pdf"))
    assert len(copied) == new_file_count
    assert rename_planner  # keep importorskip target referenced
