"""Revision schemes, ordering, and the status-change rule."""

from __future__ import annotations

import pytest

from engine.register.revision_logic import (
    ALPHA_SEQUENCE,
    RevisionComparison,
    RevisionScheme,
    clean_revision,
    compare_revisions,
    describe_change,
    detect_scheme,
    is_significant,
    parse_revision,
)

# ── Cleaning messy input ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A", "A"),
        ("Rev A", "A"),
        ("REV.A", "A"),
        ("rev_a", "A"),
        ("-A", "A"),
        ("(A)", "A"),
        ("[C]", "C"),
        ("  rev : D  ", "D"),
        ("Revision B", "B"),
        ("Issue P01", "P01"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_revision(raw, expected):
    assert clean_revision(raw) == expected


# ── Schemes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "scheme"),
    [
        ("A", RevisionScheme.ALPHA),
        ("Rev C", RevisionScheme.ALPHA),
        ("P01", RevisionScheme.PRELIMINARY),
        ("P3", RevisionScheme.PRELIMINARY),
        ("C01", RevisionScheme.CONSTRUCTION),
        ("T02", RevisionScheme.TENDER),
        ("00", RevisionScheme.NUMERIC),
        ("01", RevisionScheme.NUMERIC),
        ("A1", RevisionScheme.ALPHA),
        ("?!", RevisionScheme.UNKNOWN),
        ("", RevisionScheme.UNKNOWN),
    ],
)
def test_scheme_detection_per_code(raw, scheme):
    assert parse_revision(raw).scheme is scheme


def test_prefixed_revisions_are_normalised_to_two_digits():
    assert parse_revision("P3").normalised == "P03"
    assert parse_revision("c1").normalised == "C01"


def test_detect_scheme_over_a_set():
    assert detect_scheme(["A", "B", "C"]) is RevisionScheme.ALPHA
    assert detect_scheme(["P01", "P02", "P03"]) is RevisionScheme.PRELIMINARY
    assert detect_scheme(["00", "01"]) is RevisionScheme.NUMERIC
    assert detect_scheme([None, "", "???"]) is RevisionScheme.UNKNOWN


def test_detect_scheme_takes_the_majority_in_a_mixed_set():
    """A job part-way through moving to construction is a normal state."""
    assert detect_scheme(["C01", "C02", "C03", "P09"]) is RevisionScheme.CONSTRUCTION


# ── I and O are skipped ────────────────────────────────────────────────


def test_the_alpha_sequence_skips_i_and_o():
    """Too easily read as 1 and 0, so drawing offices never use them."""
    assert "I" not in ALPHA_SEQUENCE
    assert "O" not in ALPHA_SEQUENCE
    assert ALPHA_SEQUENCE.index("J") == ALPHA_SEQUENCE.index("H") + 1
    assert ALPHA_SEQUENCE.index("P") == ALPHA_SEQUENCE.index("N") + 1


def test_h_is_followed_by_j():
    assert compare_revisions("H", "J") is RevisionComparison.NEWER
    assert compare_revisions("J", "H") is RevisionComparison.OLDER


def test_n_is_followed_by_p():
    assert compare_revisions("N", "P") is RevisionComparison.NEWER


def test_a_revision_using_i_or_o_is_not_understood():
    """Rather than ordering it wrongly, say it cannot be compared."""
    assert parse_revision("I").scheme is RevisionScheme.UNKNOWN
    assert parse_revision("O").scheme is RevisionScheme.UNKNOWN
    assert compare_revisions("H", "I") is RevisionComparison.INCOMPARABLE


def test_ordering_after_h_is_right():
    """A plain-alphabet ordinal gets everything after H wrong."""
    letters = ["A", "B", "C", "D", "E", "F", "G", "H", "J", "K"]
    ordinals = [parse_revision(letter).ordinal for letter in letters]
    assert ordinals == sorted(ordinals)
    assert parse_revision("J").ordinal == parse_revision("H").ordinal + 1


# ── Comparison ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("A", "B", RevisionComparison.NEWER),
        ("B", "A", RevisionComparison.OLDER),
        ("C", "C", RevisionComparison.SAME),
        ("Rev C", "REV.D", RevisionComparison.NEWER),
        ("P01", "P02", RevisionComparison.NEWER),
        ("P02", "P01", RevisionComparison.OLDER),
        ("C01", "C01", RevisionComparison.SAME),
        ("00", "01", RevisionComparison.NEWER),
        ("A", None, RevisionComparison.INCOMPARABLE),
        (None, None, RevisionComparison.INCOMPARABLE),
        ("A", "???", RevisionComparison.INCOMPARABLE),
    ],
)
def test_compare_revisions(old, new, expected):
    assert compare_revisions(old, new) is expected


def test_alpha_with_a_sub_number_orders_correctly():
    assert compare_revisions("A1", "A2") is RevisionComparison.NEWER
    assert compare_revisions("A9", "B1") is RevisionComparison.NEWER


def test_two_letter_revisions_follow_z():
    assert compare_revisions("Z", "AA") is RevisionComparison.NEWER


# ── The status change rule ─────────────────────────────────────────────


def test_preliminary_to_construction_is_a_status_change_not_a_decrease():
    """P03 -> C01 looks backwards. It is not: the drawing changed status."""
    assert compare_revisions("P03", "C01") is RevisionComparison.STATUS_CHANGE
    assert compare_revisions("P03", "C01") is not RevisionComparison.OLDER


def test_any_scheme_change_is_a_status_change():
    assert compare_revisions("T02", "C01") is RevisionComparison.STATUS_CHANGE
    assert compare_revisions("A", "P01") is RevisionComparison.STATUS_CHANGE
    assert compare_revisions("01", "C01") is RevisionComparison.STATUS_CHANGE


def test_a_status_change_is_significant_even_when_the_number_drops():
    """The drawing may be identical; what it is issued for has changed."""
    assert is_significant("P03", "C01")


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("A", "B", True),  # revised
        ("P03", "C01", True),  # status change
        ("C", "C", False),  # unchanged
        ("B", "A", False),  # went backwards; flagged elsewhere, not "significant"
        ("A", None, False),  # cannot tell
    ],
)
def test_is_significant(old, new, expected):
    assert is_significant(old, new) is expected


# ── Descriptions ───────────────────────────────────────────────────────


def test_descriptions_are_plain_sentences():
    assert describe_change("A", "B") == "Revised from A to B."
    assert describe_change("C", "C") == "Still at revision C."

    backwards = describe_change("D", "C")
    assert "earlier" in backwards
    assert "Check" in backwards  # says what to do


def test_a_status_change_is_described_as_one():
    sentence = describe_change("P03", "C01")

    assert "preliminary" in sentence
    assert "construction" in sentence
    assert "not a revision decrease" in sentence


def test_a_backwards_status_change_is_called_out():
    sentence = describe_change("C01", "P01")
    assert "moved backwards" in sentence


def test_an_unreadable_revision_is_described():
    assert "could not be understood" in describe_change("A", "???")
    assert "No revision could be read" in describe_change(None, None)
