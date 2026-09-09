"""The normaliser must survive everything the B1 table throws at it.

These tests pin the Phase 3 naming behaviour: three levels of cleaning, the
Unicode lookalike trap, date/revision/prefix stripping from the ends inward
only, and the requirement that a match can always be *explained* to the user
(the NormalisationResult.stripped list).
"""

from __future__ import annotations

from engine.naming.normaliser import NormLevel, normalise, normalise_filename


def _key(name: str, level: NormLevel = NormLevel.AGGRESSIVE) -> str:
    return normalise(name, level).value


# ── LIGHT: formatting only ─────────────────────────────────────────────


def test_light_unifies_separators_and_case():
    for name in ("UVU-ARC-001", "UVU_ARC_001", "UVU ARC 001", "uvu-arc-001"):
        assert normalise(name, NormLevel.LIGHT).value == "uvu-arc-001"


def test_light_keeps_junk_and_extension():
    assert normalise("A-101-RevC.pdf", NormLevel.LIGHT).value == "a-101-revc-pdf"


def test_light_maps_unicode_lookalikes():
    # En dash, em dash, non-breaking space — all look identical on screen.
    assert _key("UVU\u2013ARC\u2013001") == _key("UVU-ARC-001")
    assert _key("UVU\u2014ARC\u2013001") == _key("UVU-ARC-001")
    assert _key("UVU\u00a0ARC\u00a0001") == _key("UVU-ARC-001")
    assert _key("UVU\u2011ARC\u2011001") == _key("UVU-ARC-001")


def test_light_handles_nfkc_decomposition():
    # Full-width digits and letters decompose under NFKC.
    assert normalise("UVU\uff0dARC\uff0d001", NormLevel.LIGHT).value == "uvu-arc-001"


def test_light_collapses_whitespace_runs():
    assert normalise("UVU  ARC    001", NormLevel.LIGHT).value == "uvu-arc-001"


def test_light_maps_curly_quotes_to_straight():
    assert "\u2018" not in normalise("\u2018copy\u2019 of UVU-ARC-001", NormLevel.LIGHT).value
    assert _key("\u2018copy\u2019 of UVU-ARC-001 ") == _key("copy of UVU-ARC-001")


# ── MEDIUM: extension, revision, dates, prefixes ───────────────────────


def test_medium_strips_the_extension():
    result = normalise("A-101.pdf", NormLevel.MEDIUM)
    assert result.value == "a-101"
    assert result.stripped == ["the file extension .pdf"]


def test_medium_removes_revision_suffixes():
    for name in (
        "A-101-RevC.pdf",
        "A-101_RevC.pdf",
        "A-101-Rev-C.pdf",
        "A-101_Rev D.pdf",
        "A-101-RevD.pdf",
        "A-101_Rev01.pdf",
        "A-101-REV-01.pdf",
    ):
        assert normalise(name, NormLevel.MEDIUM).value == "a-101", name


def test_medium_removes_parenthesised_revisions():
    assert normalise("A-101 (C).pdf", NormLevel.MEDIUM).value == "a-101"
    assert normalise("A-101(C02).pdf", NormLevel.MEDIUM).value == "a-101"


def test_medium_removes_underscore_revision_codes():
    assert normalise("A-101_D.pdf", NormLevel.MEDIUM).value == "a-101"
    assert normalise("A-101_P01.pdf", NormLevel.MEDIUM).value == "a-101"
    assert normalise("A-101_C02.pdf", NormLevel.MEDIUM).value == "a-101"


def test_medium_does_not_call_a_sheet_size_a_revision():
    # _A1 is a sheet size marker, not a revision code.
    assert normalise("A-101_A1.pdf", NormLevel.MEDIUM).value == "a-101-a1"
    assert "revision" not in " ".join(normalise("A-101_A1.pdf", NormLevel.MEDIUM).stripped)


def test_medium_removes_date_stamps():
    for name in (
        "A-101_2026-04-12.pdf",
        "A-101-12.04.26.pdf",
        "A-101-12/04/2026.pdf",
        "A-101-20260412.pdf",
        "A-101-12-Apr-2026.pdf",
        "A-101-Apr-2026.pdf",
        "A-101-12 Apr 2026.pdf",
    ):
        assert normalise(name, NormLevel.MEDIUM).value == "a-101", name


def test_medium_removes_sequence_prefixes():
    assert normalise("01_UVU-ARC-001.pdf", NormLevel.MEDIUM).value == "uvu-arc-001"
    assert normalise("001 - UVU-ARC-001.pdf", NormLevel.MEDIUM).value == "uvu-arc-001"
    assert normalise("12.UVU-ARC-001.pdf", NormLevel.MEDIUM).value == "uvu-arc-001"


def test_medium_strips_never_from_the_middle():
    # The 01 in UVU-ARC-001 is part of the number and must survive, and the
    # sequence-prefix rule must not eat a number that legitimately leads.
    assert normalise("UVU-ARC-001.pdf", NormLevel.MEDIUM).value == "uvu-arc-001"
    assert normalise("A-001-RevC.pdf", NormLevel.MEDIUM).value == "a-001"
    # 1234-A-101 starts with four digits: longer than a sequence prefix.
    assert normalise("1234-A-101-RevC.pdf", NormLevel.MEDIUM).value == "1234-a-101"


def test_medium_combines_suffixes_in_one_pass():
    result = normalise("001 - UVU-ARC-001 - 12.04.2026_RevD.pdf", NormLevel.MEDIUM)
    assert result.value == "uvu-arc-001"
    kinds = " ".join(result.stripped).lower()
    assert "sequence prefix" in kinds
    assert "date stamp" in kinds
    assert "revision suffix" in kinds


# ── AGGRESSIVE: status words, copy junk, sizes, alphanumerics only ─────


def test_aggressive_keeps_only_alphanumerics():
    value = normalise("UVU-KEO-XX-03-DR-A-0001.pdf", NormLevel.AGGRESSIVE).value
    assert value == "uvukeoxx03dra0001"


def test_aggressive_removes_status_words():
    assert _key("A-101_FOR APPROVAL.pdf") == "a101"
    assert _key("A-101-IFC.pdf") == "a101"
    assert _key("A-101_SUPERSEDED.pdf") == "a101"
    assert _key("A-101_PRELIMINARY.pdf") == "a101"
    assert _key("A-101_RevD_FOR APPROVAL.pdf") == "a101"


def test_aggressive_removes_copy_junk():
    assert _key("Copy of UVU-ARC-001.pdf") == _key("UVU-ARC-001.pdf")
    assert _key("UVU ARC 001 (1).pdf") == _key("UVU-ARC-001.pdf")
    assert _key("UVU-ARC-001 - Copy.pdf") == _key("UVU-ARC-001.pdf")


def test_aggressive_removes_human_junk():
    for junk in ("FINAL", "final_final", "new", "USE THIS ONE"):
        assert _key(f"UVU-ARC-001_{junk}.pdf") == "uvuarc001", junk


def test_aggressive_removes_sheet_size_markers():
    assert _key("UVU-ARC-001_A1.pdf") == "uvuarc001"
    assert _key("UVU-ARC-001_A0.pdf") == "uvuarc001"


def test_aggressive_records_what_it_stripped():
    result = normalise("UVU-ARC-001_RevD_FOR APPROVAL.pdf", NormLevel.AGGRESSIVE)
    assert "revision suffix" in " ".join(result.stripped)
    assert any("approval" in part or "for" in part for part in result.stripped)


# ── The B1 table, end to end ───────────────────────────────────────────


def test_every_b1_change_collapses_to_the_same_key():
    """Each row of the B1 table: a change of format, never of drawing."""
    base = _key("UVU-ARC-001.pdf")
    variants = [
        "UVU_ARC_001.pdf",  # separator swap
        "uvu-arc-001.pdf",  # case change
        "UVU-ARC-001_RevD.pdf",  # revision suffix added
        "UVU-ARC-001-Rev-D.pdf",  # revision format changed
        "UVU-ARC-001 (D).pdf",
        "UVU-ARC-001_D.pdf",
        "UVU-ARC-001_2026-04-12.pdf",  # date stamp added
        "UVU-ARC-001-12.04.26.pdf",  # date format varies
        "UVU-ARC-001-12-Apr-2026.pdf",
        "01_UVU-ARC-001.pdf",  # sequence prefix added
        "001 - UVU-ARC-001.pdf",
        "UVU-ARC-001_FOR APPROVAL.pdf",  # status words added
        "UVU-ARC-001_SUPERSEDED.pdf",
        "UVU-ARC-001_IFC.pdf",
        "Copy of UVU-ARC-001.pdf",  # Windows copy junk
        "UVU ARC 001 (1).pdf",
        "UVU-ARC-001 - Copy.pdf",
        "UVU-ARC-001_FINAL.pdf",  # human junk
        "UVU-ARC-001_final_final.pdf",
        "UVU-ARC-001_A1.pdf",  # sheet size marker
        "UVU\u2013ARC\u2013001.pdf",  # Unicode lookalikes
        "UVU\u2013ARC\u2013001\u00a0.pdf",
        "UVU-ARC-001 .pdf",  # trailing dot or space
        "UVU-ARC-001 .PDF",
    ]
    for variant in variants:
        assert _key(variant) == base, variant


def test_trailing_dots_and_spaces_are_handled():
    assert _key("UVU-ARC-001 .pdf") == "uvuarc001"
    assert _key("UVU-ARC-001..pdf") == "uvuarc001"


def test_medium_then_aggressive_matches_the_mess_folder():
    """The 08_naming_mess folder is full of variants of UVU-ARC-001. AGGRESSIVE
    must collapse every variant to one key; MEDIUM handles the formatting
    differences (separators, revision, dates) and leaves the junk alone."""
    names = [
        "Copy of UVU-ARC-001.pdf",
        "UVU ARC 001 (1).pdf",
        "UVU_ARC_001_RevD_FOR APPROVAL.pdf",
        "01 - UVU-ARC-001 - 12.04.2026.pdf",
        "uvu-arc-001 FINAL final.pdf",
        "UVU\u2013ARC\u2013001.pdf",
    ]
    aggressive = {normalise_filename(name, NormLevel.AGGRESSIVE).value for name in names}
    assert aggressive == {"uvuarc001"}

    medium_clean = [
        "uvu-arc-001.pdf",
        "UVU_ARC_001.pdf",
        "UVU-ARC-001_RevD.pdf",
        "UVU-ARC-001-12.04.2026.pdf",
        "01 - UVU-ARC-001.pdf",
        "UVU\u2013ARC\u2013001.pdf",
    ]
    medium = {normalise_filename(name, NormLevel.MEDIUM).value for name in medium_clean}
    assert medium == {"uvu-arc-001"}


def test_reason_reads_as_english():
    result = normalise("A-101-RevC.pdf", NormLevel.MEDIUM)
    assert "revision suffix" in result.reason()
    assert normalise("A-101", NormLevel.MEDIUM).reason() == "the name needed no cleaning"


def test_empty_name_is_safe():
    assert normalise("", NormLevel.AGGRESSIVE).value == ""
    assert normalise(None, NormLevel.LIGHT).value == ""  # type: ignore[arg-type]
