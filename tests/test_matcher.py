"""Matcher tests. The hard rule: zero wrong pairs; unmatched is honest.

Covers the five tiers, the greedy-vs-optimal-assignment proof, ambiguity
handling, and an end-to-end naming-standard-change scenario on real PDFs.
"""

from __future__ import annotations

from engine.core.models import SheetRecord
from engine.naming import matcher as m
from engine.naming.fingerprint import SheetFingerprint, build_document_fingerprints
from engine.naming.matcher import (
    TIER_EXACT,
    TIER_FINGERPRINT,
    TIER_NORMALISED_NAME,
    TIER_NORMALISED_NUMBER,
    match_sets,
)
from tests.fixture_builder import SheetSpec, build_pdf


def sheet(
    filename: str,
    *,
    drawing_no: str | None = None,
    number_source: str = "titleblock",
    revision: str | None = None,
    title: str | None = None,
) -> SheetRecord:
    return SheetRecord(
        side="old",
        abs_path=f"C:/issue/{filename}",
        filename=filename,
        drawing_no=drawing_no,
        normalised_no="".join(character for character in (drawing_no or "").upper() if character.isalnum()),
        source_of_number=number_source,
        revision=revision,
        title=title,
    )


def _fp(tokens: set[str]) -> SheetFingerprint:
    return SheetFingerprint(
        page_size_mm=(840.0, 595.0),
        orientation="landscape",
        text_tokens=frozenset(tokens),
        path_bucket=1,
        grid_labels=(),
        text_hash="",
    )


def _fp_sim(left: SheetFingerprint, right: SheetFingerprint) -> float:
    from engine.naming.fingerprint import similarity

    return similarity(left, right)


def _fp_resolver(by_path: dict[str, SheetFingerprint]):
    def resolve(sheet: SheetRecord) -> SheetFingerprint | None:
        return by_path.get(sheet.abs_path)
    return resolve


def test_tier1_pairs_identical_drawing_numbers():
    old = [sheet("whatever.pdf", drawing_no="UVU-ARC-001")]
    new = [sheet("something-else.pdf", drawing_no="UVU-ARC-001")]
    result = match_sets(old, new)
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.tier == TIER_EXACT
    assert pair.confidence == 1.0
    assert not result.old_unmatched and not result.new_unmatched


def test_tier2_pairs_numbers_that_differ_only_in_formatting():
    old = [sheet("x.pdf", drawing_no="UVU-ARC-001")]
    new = [sheet("y.pdf", drawing_no="UVU ARC 001")]
    result = match_sets(old, new)
    assert len(result.pairs) == 1
    assert result.pairs[0].tier == TIER_NORMALISED_NUMBER
    assert result.pairs[0].confidence == 0.98


def test_tier3_pairs_by_normalised_filename_when_no_numbers():
    old = [sheet("UVU-ARC-001_RevD.pdf")]
    new = [sheet("uvu arc 001.pdf")]
    result = match_sets(old, new)
    assert len(result.pairs) == 1
    assert result.pairs[0].tier == TIER_NORMALISED_NAME
    assert result.pairs[0].reason  # the UI shows why


def test_ambiguous_same_key_is_left_unmatched():
    """Two new sheets cleaning to the same filename key: never guess."""
    old = [sheet("UVU-ARC-001-RevD.pdf", drawing_no=None)]
    new = [
        sheet("uvu arc 001.pdf"),
        sheet("UVU_ARC_001-12.04.2026.pdf"),
    ]
    result = match_sets(old, new)
    assert result.pairs == []
    assert len(result.old_unmatched) == 1


def test_superseded_duplicates_are_not_matched_twice():
    old = [
        sheet("A-101-RevB.pdf", drawing_no="A-101", revision="B"),
        sheet("A-101-RevC.pdf", drawing_no="A-101", revision="C"),
    ]
    new = [sheet("A-101-RevD.pdf", drawing_no="A-101", revision="D")]
    result = match_sets(old, new)
    assert len(result.pairs) == 1
    assert result.pairs[0].old.filename == "A-101-RevC.pdf"
    assert len(result.superseded) == 1


def test_greedy_would_fail_and_optimal_assignment_wins():
    """B4 proof: a greedy loop (X first) pairs X->P, then gives Y->Q and calls
    it done. The global solve sees that Y->P is far stronger than Y->Q and
    pairs X->Q, Y->P instead — the correct answer.

    Scores used:  X->P .936, X->Q .817, Y->P .970, Y->Q .758. A greedy loop
    picks X->P and then Y->Q (both above threshold, so it reports success);
    the assignment picks Y->P and X->Q.
    """
    old_x = sheet("xqx-gibberish-17.pdf", drawing_no="101")
    old_y = sheet("yqy-gibberish-29.pdf", drawing_no="102")
    new_p = sheet("pnp-qux-plumb-99.pdf", drawing_no="201")
    new_q = sheet("qnq-quux-plumb-100.pdf", drawing_no="202")

    x_tokens = set(range(1, 21))  # 1..20
    p_tokens = {*range(1, 21), 51, 52}
    q_tokens = {*range(1, 18), 61, 62, 63}  # 1..17 + 61..63
    y_tokens = {*range(1, 21), 51, 52, 71}

    fingerprints = {
        old_x.abs_path: _fp({str(t) for t in x_tokens}),
        old_y.abs_path: _fp({str(t) for t in y_tokens}),
        new_p.abs_path: _fp({str(t) for t in p_tokens}),
        new_q.abs_path: _fp({str(t) for t in q_tokens}),
    }

    # Sanity: the scores behave as the proof needs them to.
    score = _fp_sim
    assert score(fingerprints[old_x.abs_path], fingerprints[new_p.abs_path]) > score(
        fingerprints[old_x.abs_path], fingerprints[new_q.abs_path]
    )
    assert score(fingerprints[old_y.abs_path], fingerprints[new_p.abs_path]) > score(
        fingerprints[old_x.abs_path], fingerprints[new_p.abs_path]
    )

    result = match_sets(
        [old_x, old_y], [new_p, new_q], fingerprint_for=_fp_resolver(fingerprints)
    )
    by_old = {pair.old.filename: pair.new.filename for pair in result.pairs}
    # Greedy would give P to X (X scores .936) and leave Y with the wrong Q.
    assert by_old == {
        "yqy-gibberish-29.pdf": "pnp-qux-plumb-99.pdf",
        "xqx-gibberish-17.pdf": "qnq-quux-plumb-100.pdf",
    }
    assert not result.old_unmatched
    assert not result.new_unmatched


def test_below_review_threshold_is_unmatched_not_forced():
    old = [sheet("zzold.pdf", drawing_no="1")]
    new = [sheet("qqnew.pdf", drawing_no="2")]
    fingerprints = {
        old[0].abs_path: _fp({"only", "these", "words"}),
        new[0].abs_path: _fp({"completely", "different", "ones", "here"}),
    }
    result = match_sets(old, new, fingerprint_for=_fp_resolver(fingerprints))
    assert result.pairs == []
    assert len(result.old_unmatched) == 1
    assert len(result.new_unmatched) == 1


def _similarity_pair(tokens_a: set[int], tokens_b: set[int]) -> float:
    left = _fp({str(token) for token in tokens_a})
    right = _fp({str(token) for token in tokens_b})
    return _fp_sim(left, right)


def test_standard_change_pairing_on_real_pdfs(tmp_path):
    """03_renamed core scenario, through the whole pipeline.

    Old and new sides carry the SAME four drawings; only the drawing number
    standard changed (UVU-ARC-00x vs UVU-KEO-XX-03-DR-A-000x). Number tiers
    fail, name tiers fail; the fingerprint tier must pair them, and it must
    pair each old sheet to its own new version, not to a neighbour.
    """
    drawings = [
        ("UVU-ARC-001", "GROUND FLOOR PLAN", ["ROOM 101 LAYOUT", "WALL W1", "3000 CEILING"]),
        ("UVU-ARC-002", "FIRST FLOOR PLAN", ["ROOM 201 LAYOUT", "WALL W2", "3100 CEILING"]),
        ("UVU-ARC-003", "ROOF PLAN", ["ROOF SLOPE 5 DEGREES", "FALL TO OUTLET"]),
        ("UVU-ARC-004", "SECTION A-A", ["EXTERNAL WALL BUILD UP", "CLADDING PANEL"]),
    ]
    mapping = {
        "UVU-ARC-001": "UVU-KEO-XX-03-DR-A-0001",
        "UVU-ARC-002": "UVU-KEO-XX-03-DR-A-0002",
        "UVU-ARC-003": "UVU-KEO-XX-03-DR-A-0003",
        "UVU-ARC-004": "UVU-KEO-XX-03-DR-A-0004",
    }
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    for number, title, body in drawings:
        build_pdf(
            old_dir / f"{number}_{title.replace(' ', '_')}_RevC.pdf",
            [SheetSpec(drawing_no=number, title=title, body=body, revision="C")],
        )
        new_number = mapping[number]
        build_pdf(
            new_dir / f"{new_number}_RevD.pdf",
            [SheetSpec(drawing_no=new_number, title=title, body=body, revision="D")],
        )

    def load(folder, source: str) -> list[SheetRecord]:
        sheets: list[SheetRecord] = []
        for pdf in sorted(folder.glob("*.pdf")):
            pages = build_document_fingerprints(str(pdf))
            assert pages, f"no fingerprint for {pdf.name}"
            sheets.append(
                SheetRecord(
                    side=source,
                    abs_path=str(pdf),
                    filename=pdf.name,
                    page_count=1,
                    drawing_no=None,  # let the matcher ignore the numbers entirely
                )
            )
        return sheets

    old_sheets = load(old_dir, "old")
    new_sheets = load(new_dir, "new")
    assert len(old_sheets) == len(new_sheets) == 4

    result = match_sets(
        old_sheets, new_sheets, fingerprint_for=_resolver_for([*old_sheets, *new_sheets])
    )
    assert len(result.pairs) == 4, result.summary()
    assert not result.old_unmatched and not result.new_unmatched

    # Every pair must join the two versions of ONE drawing. The expected map
    # is positional: sorted old files correspond 1:1 to sorted new files.
    for pair in result.pairs:
        assert pair.tier == TIER_FINGERPRINT
        old_index = old_sheets.index(pair.old)
        assert pair.new is new_sheets[old_index], f"wrong pair: {pair.old.filename}"


def _resolver_for(sheets: list[SheetRecord]):
    cache: dict[str, dict[int, SheetFingerprint]] = {}
    for sheet in sheets:
        if sheet.abs_path not in cache:
            cache[sheet.abs_path] = build_document_fingerprints(sheet.abs_path)

    def resolve(target: SheetRecord) -> SheetFingerprint | None:
        return cache.get(target.abs_path, {}).get(target.page_index)

    return resolve


def test_summary_counts_auto_and_review():
    old = [sheet("UVU-ARC-001.pdf", drawing_no="UVU-ARC-001")]
    new = [sheet("UVU-ARC-001_RevD.pdf", drawing_no="UVU-ARC-001")]
    result = match_sets(old, new)
    assert result.summary()["auto"] == 1
