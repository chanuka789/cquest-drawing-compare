"""Template engine tests: ISO 19650 rendering, missing tokens, modifiers,
Windows sanitisation, and the shipped presets.
"""

from __future__ import annotations

from engine.core.models import SheetRecord
from engine.naming.template import (
    ParsedNumber,
    available_templates,
    is_reserved_name,
    load_template,
    parse_drawing_number,
    preview,
    render,
    sanitise_name,
)


def sheet(
    filename: str,
    *,
    drawing_no: str | None = None,
    title: str | None = None,
    revision: str | None = None,
) -> SheetRecord:
    return SheetRecord(
        abs_path=f"C:/issue/{filename}",
        filename=filename,
        drawing_no=drawing_no,
        title=title,
        revision=revision,
    )


CONTEXT = {"project": "UVU", "originator": "KEO"}


def test_parse_iso_number_splits_into_fields():
    parsed = parse_drawing_number("UVU-KEO-XX-03-DR-A-0001")
    assert parsed.get("number") == "0001"
    assert parsed.get("role") == "A"
    assert parsed.get("type") == "DR"
    assert parsed.get("level") == "03"
    assert parsed.get("volume") == "XX"
    assert parsed.get("originator") == "KEO"
    assert parsed.get("project") == "UVU"


def test_parse_old_style_number_finds_number_and_role():
    parsed: ParsedNumber = parse_drawing_number("UVU-ARC-001")
    assert parsed.get("number") == "001"
    assert parsed.fields  # something was read


def test_render_iso_template_from_iso_number():
    target = sheet("file.pdf", drawing_no="UVU-KEO-XX-03-DR-A-0001", title="GROUND FLOOR PLAN")
    result = render(
        "{project}-{originator}-{volume}-{level}-{type}-{role}-{number}_{rev}", target, CONTEXT
    )
    assert result.empty is False
    assert result.filename == "UVU-KEO-XX-03-DR-A-0001.pdf"
    assert result.missing == ["rev"]  # no revision anywhere on this sheet


def test_render_never_leaves_a_literal_brace_token():
    target = sheet("A-101.pdf", drawing_no="A-101")
    result = render("{project}-{originator}-{number}_{rev}", target, CONTEXT)
    assert "{rev}" not in result.filename
    assert result.filename == "UVU-KEO-101.pdf"
    assert "rev" in result.missing


def test_revision_is_kept_when_present():
    target = sheet("A-101.pdf", drawing_no="A-101", revision="D")
    result = render("{project}-{originator}-{number}_{rev}", target, CONTEXT)
    assert result.filename == "UVU-KEO-101_D.pdf"
    assert result.missing == []


def test_title_and_date_tokens_resolve_from_sheet_and_filename():
    target = sheet(
        "UVU-ARC-001 12.04.2026.pdf",
        drawing_no="UVU-ARC-001",
        title="GROUND FLOOR PLAN",
    )
    result = render("{title}_{date}", target, CONTEXT)
    assert result.filename.startswith("GROUND FLOOR PLAN_")
    assert "12-04-2026" in result.filename or "12.04.2026" in result.filename


def test_original_token_keeps_the_stem():
    target = sheet("SITE-PLAN-RevC-00123.pdf", drawing_no="A-101")
    result = render("{original}_{rev}", target, CONTEXT)
    # The revision is picked up from the file name itself.
    assert result.filename == "SITE-PLAN-RevC-00123_C.pdf"


def test_keep_original_preset_appends_revision():
    target = sheet("SITE-PLAN.pdf", revision="B")
    result = render("{original}_{rev}", target, CONTEXT)
    assert result.filename == "SITE-PLAN_B.pdf"


def test_modifiers_truncate_upper_and_pad():
    target = sheet("x.pdf", title="GROUND FLOOR REFLECTED CEILING PLAN", revision="d")
    result = render("{title:10}", target, CONTEXT)
    assert result.filename.startswith("GROUND FL")
    result = render("{rev:upper}", target, CONTEXT)
    assert result.filename == "D.pdf"
    result = render("{number:pad5}", sheet("x.pdf", drawing_no="A-001"), {**CONTEXT, "number": "1"})
    assert result.filename == "00001.pdf"


def test_invalid_windows_characters_are_stripped():
    assert sanitise_name('Plan "final": ver 1 <2> ?*') == "Plan final ver 1 2"


def test_trailing_dots_and_spaces_stripped():
    assert sanitise_name("UVU-ARC-001 . ") == "UVU-ARC-001"


def test_reserved_device_names_are_detected():
    for name in ("CON", "con.pdf", "PRN", "AUX", "NUL", "COM1", "com9", "LPT5", "lpt1.x"):
        assert is_reserved_name(name), name
    assert not is_reserved_name("A-101")
    assert not is_reserved_name("CONTRACT")


def test_preview_returns_real_examples():
    sheets = [
        sheet("UVU-ARC-001-RevC.pdf", drawing_no="UVU-ARC-001"),
        sheet("UVU-ARC-002-RevC.pdf", drawing_no="UVU-ARC-002"),
    ]
    names = preview("{project}-{originator}-{number}", sheets, CONTEXT)
    assert names == ["UVU-KEO-001.pdf", "UVU-KEO-002.pdf"]


def test_shipped_presets_exist_and_load():
    templates = available_templates()
    ids = {template.id for template in templates}
    assert {"iso19650", "keep_original_add_rev", "custom"} <= ids
    iso = load_template("iso19650")
    assert iso is not None
    assert iso.template.startswith("{project}")


def test_custom_template_can_be_empty_without_crashing():
    target = sheet("A-101.pdf", drawing_no="A-101")
    result = render("", target, CONTEXT)
    assert result.empty is True
