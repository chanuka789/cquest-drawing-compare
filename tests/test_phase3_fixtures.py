"""The Phase 3 golden fixtures stay golden.

`03_renamed`, `08_naming_mess` and `09_collision` are built on demand into a
tmp dir from :mod:`tests.fixture_builder`, so the naming and fingerprint
machinery is pinned by assertions here rather than by files that live outside
the repository. A builder change that breaks the golden expectations (matched
pairs under 0.85, different drawings at or over 0.4, a renamed drawing paired
to the wrong sheet) fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from engine.core.models import SheetRecord
from engine.naming.fingerprint import (
    SheetFingerprint,
    build_document_fingerprints,
    similarity,
)
from engine.naming.matcher import TIER_FINGERPRINT, match_sets
from engine.naming.normaliser import NormLevel, normalise
from tests.fixture_builder import (
    RENAMED_NEW_ONLY,
    RENAMED_OLD_ONLY,
    RENAMED_SET,
    build_collision_folder,
    build_naming_mess,
    build_renamed_pair,
)


def _old_name(number: str, title: str) -> str:
    return f"{number}_{title.replace(' ', '_')}_RevC.pdf"


def _new_name(number: str) -> str:
    return f"{number}_RevC.pdf"


def _load(folder: Path, side: str) -> list[SheetRecord]:
    return [
        SheetRecord(side=side, abs_path=str(pdf), filename=pdf.name)
        for pdf in sorted(folder.glob("*.pdf"))
    ]


def _fingerprints(folder: Path) -> dict[str, SheetFingerprint]:
    """Page-0 fingerprint per file, keyed by the same path the records use."""
    cache: dict[str, SheetFingerprint] = {}
    for pdf in sorted(folder.glob("*.pdf")):
        pages = build_document_fingerprints(str(pdf))
        assert pages, f"no fingerprint for {pdf.name}"
        cache[str(pdf)] = pages[0]
    return cache


def _fingerprint_resolver(fingerprints: dict[str, SheetFingerprint]):
    def resolve(sheet: SheetRecord) -> SheetFingerprint | None:
        return fingerprints.get(sheet.abs_path)

    return resolve


def test_renamed_pair_fingerprints_separate_drawings(tmp_path):
    """03_renamed: one drawing scores like itself, never like a neighbour."""
    old_dir, new_dir = build_renamed_pair(tmp_path / "03_renamed")
    old_fps = _fingerprints(old_dir)
    new_fps = _fingerprints(new_dir)
    assert len(old_fps) == len(new_fps) == len(RENAMED_SET) + 1

    # The two versions of one drawing carry the same body, title and sheet.
    for old_no, new_no, title, _body in RENAMED_SET:
        left = old_fps[str(old_dir / _old_name(old_no, title))]
        right = new_fps[str(new_dir / _new_name(new_no))]
        assert similarity(left, right) >= 0.85, (old_no, new_no)

    # A drawing never looks like a different drawing.
    expected: set[tuple[str, str]] = {
        (str(old_dir / _old_name(old_no, title)), str(new_dir / _new_name(new_no)))
        for old_no, new_no, title, _body in RENAMED_SET
    }
    for old_path, left in old_fps.items():
        for new_path, right in new_fps.items():
            if (old_path, new_path) in expected:
                continue
            score = similarity(left, right)
            assert score < 0.4, (old_path, new_path, score)


def test_renamed_pair_matcher_zero_wrong_pairs(tmp_path):
    """03_renamed end to end: every old drawing finds its own new version.

    The number tiers fail (the standard changed), the name tiers fail (the
    names changed), so only the fingerprint tier can pair them — and it must
    pair each sheet to its own version, leave the old-only and new-only
    drawings honestly unmatched, and never invent a wrong pair.
    """
    old_dir, new_dir = build_renamed_pair(tmp_path / "03_renamed")
    old_sheets = _load(old_dir, "old")
    new_sheets = _load(new_dir, "new")
    assert len(old_sheets) == len(new_sheets) == len(RENAMED_SET) + 1

    fingerprints = {**_fingerprints(old_dir), **_fingerprints(new_dir)}
    result = match_sets(old_sheets, new_sheets, fingerprint_for=_fingerprint_resolver(fingerprints))

    expected = {
        _old_name(old_no, title): _new_name(new_no) for old_no, new_no, title, _body in RENAMED_SET
    }
    paired = {pair.old.filename: pair.new.filename for pair in result.pairs}
    assert paired == expected

    for pair in result.pairs:
        assert pair.tier == TIER_FINGERPRINT, pair.reason
        assert pair.confidence >= 0.85, pair.confidence
        assert not pair.needs_review

    assert [sheet.filename for sheet in result.old_unmatched] == [
        _old_name(RENAMED_OLD_ONLY[0], RENAMED_OLD_ONLY[1])
    ]
    assert [sheet.filename for sheet in result.new_unmatched] == [_new_name(RENAMED_NEW_ONLY[0])]


def test_naming_mess_every_name_cleans_to_one_key(tmp_path):
    """08_naming_mess: every naming sin still lands on ``uvuarc001``."""
    folder = build_naming_mess(tmp_path / "08_naming_mess")
    files = sorted(folder.glob("*.pdf"))
    assert len(files) == 7
    for pdf in files:
        assert normalise(pdf.name, NormLevel.AGGRESSIVE).value == "uvuarc001", pdf.name


def test_collision_folder_names_collide_but_drawings_differ(tmp_path):
    """09_collision: the planner-level collision is real, the drawings are not.

    Both file names clean to the same MEDIUM key, so a naive rename would send
    them to one target — but the title blocks and body text make them two
    different drawings, which their fingerprints prove.
    """
    folder = build_collision_folder(tmp_path / "09_collision")
    files = sorted(folder.glob("*.pdf"))
    assert len(files) == 2

    keys = [normalise(pdf.name, NormLevel.MEDIUM).value for pdf in files]
    assert keys[0] == keys[1] == "uvu-arc-001"

    fingerprints = _fingerprints(folder)
    left, right = [fingerprints[str(pdf)] for pdf in files]
    assert left.text_tokens.isdisjoint(right.text_tokens)
    assert similarity(left, right) < 0.4
