"""Token-based rename templates, ISO 19650 first.

The construction industry already has a naming standard, so the app does not
invent one:

    Project - Originator - Volume/System - Level/Location - Type - Role - Number
    UVU     - KEO        - XX            - 03             - DR   - A    - 0001

with an optional revision suffix: ``UVU-KEO-XX-03-DR-A-0001_D``.

This module renders a template string like
``{project}-{originator}-{volume}-{level}-{type}-{role}-{number}_{rev}`` into
a real file name for a sheet, resolving each ``{token}`` from the sheet's
drawing number, revision, title and file name, plus project-level settings.

Two rules keep the output safe:

* **Never write a literal ``{rev}`` into a file name.** A token that cannot
  be resolved is dropped, together with its separator. ``A-0001`` without a
  revision renders as ``UVU-KEO-XX-03-DR-A-0001``, never as
  ``UVU-KEO-XX-03-DR-A-0001_{rev}``.
* **Every output is sanitised for Windows** before it is returned: invalid
  characters stripped, trailing dots and spaces removed, reserved device
  names detected. The rename planner builds on these guarantees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine.core.models import SheetRecord
from engine.storage.paths import bundle_root, get_app_paths

#: Tokens every template may use. Shown as chips in the rename screen.
AVAILABLE_TOKENS: tuple[str, ...] = (
    "project",
    "originator",
    "volume",
    "level",
    "type",
    "role",
    "number",
    "rev",
    "title",
    "date",
    "status",
    "original",
)

#: Characters Windows never allows in a file name.
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Reserved device names, with or without an extension.
_RESERVED_DEVICES = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)

#: Status words that may ride on a received file name.
_STATUS_WORDS: tuple[tuple[str, str], ...] = (
    ("ifc", "IFC"),
    ("ifa", "IFA"),
    ("ift", "IFT"),
    ("for approval", "IFC"),
    ("for construction", "IFC"),
    ("superseded", "SUP"),
    ("preliminary", "PRE"),
    ("issued for construction", "IFC"),
)

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE_IN_FILENAME = re.compile(
    r"\d{4}[-_./]\d{1,2}[-_./]\d{1,2}|\d{1,2}[-_./]\d{1,2}[-_./]\d{2,4}|"
    r"\d{1,2}[-_ ]?" + _MONTHS + r"[-_ ]?\d{2,4}",
    re.IGNORECASE,
)

#: A `{token:modifier}` inside the template.
_TOKEN_PATTERN = re.compile(r"\{([a-z]+)(?::([a-z0-9]+))?\}")

#: Rightmost digits group: the drawing number in most old naming schemes.
_NUMBER_TAIL = re.compile(r"(\d{3,6})$")

#: A letter immediately before the number tail: the role code.
_ROLE_BEFORE_NUMBER = re.compile(r"([A-Z])[^A-Z0-9]*(\d{3,6})$", re.IGNORECASE)


@dataclass(slots=True)
class ParsedNumber:
    """A drawing number split into ISO 19650 fields, where they are readable."""

    fields: dict[str, str | None] = field(default_factory=dict)
    #: Whole tokens of the number that did not map to any ISO field.
    leftover: list[str] = field(default_factory=list)

    def get(self, token: str) -> str | None:
        return self.fields.get(token)

    @property
    def is_empty(self) -> bool:
        return not self.fields and not self.leftover


@dataclass(slots=True)
class RenderResult:
    """One rendered file name, plus what could not be resolved."""

    #: New file name including the original extension.
    filename: str
    #: Tokens that were in the template but had no value for this sheet.
    missing: list[str] = field(default_factory=list)
    #: Token -> value actually used, for the review table and audit log.
    used: dict[str, str] = field(default_factory=dict)
    #: True when the rendered stem was empty (nothing usable to name it).
    empty: bool = False

    @property
    def warning(self) -> str | None:
        if self.empty:
            return "No usable name could be built for this file."
        if self.missing:
            joined = ", ".join(f"{{{token}}}" for token in self.missing)
            return f"Not available: {joined}."
        return None


@dataclass(slots=True)
class NamingTemplate:
    """One naming template preset or a user's custom template."""

    id: str
    label: str
    template: str
    description: str = ""

    def render(self, sheet: SheetRecord, context: dict[str, str]) -> RenderResult:
        return render(self.template, sheet, context)

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "template": self.template,
            "description": self.description,
        }


# ── Parsing an existing drawing number ─────────────────────────────────


def parse_drawing_number(number: str | None, pattern: str | None = None) -> ParsedNumber:
    """Split *number* into ISO 19650 fields.

    `pattern` is an optional anchored regex with named groups (originator,
    volume, level, type, role, number). Without one, numbers that already look
    ISO 19650-shaped are split structurally, and anything else falls back to a
    best-effort parse: the trailing digits are the number and the remaining
    dash-separated tokens are assigned volume/level/type from the right where
    they look like codes.
    """
    if not number:
        return ParsedNumber()
    cleaned = str(number).strip()

    if pattern:
        match = re.fullmatch(pattern, cleaned, re.IGNORECASE)
        if match:
            fields = {name: value for name, value in match.groupdict().items() if value}
            return ParsedNumber(fields=fields)

    iso = re.fullmatch(
        r"(?:(?P<project>[A-Z0-9]{2,6})-)?(?P<originator>[A-Z0-9]{2,6})-"
        r"(?P<volume>[A-Z0-9]{2,6})-(?P<level>[A-Z0-9]{1,4})-"
        r"(?P<type>[A-Z]{2,3})-(?P<role>[A-Z])-(?P<number>\d{3,6})",
        cleaned,
        re.IGNORECASE,
    )
    if iso:
        return ParsedNumber(
            fields={name: value for name, value in iso.groupdict().items() if value}
        )

    return _structural_parse(cleaned)


def _structural_parse(number: str) -> ParsedNumber:
    tokens = [token for token in re.split(r"[-_ .]+", number) if token]
    parsed = ParsedNumber()
    if not tokens:
        return parsed

    tail = _NUMBER_TAIL.search(number)
    number_value = tail.group(1) if tail else None
    if number_value:
        parsed.fields["number"] = number_value
        head = number[: tail.start()].rstrip("-_ .")
    else:
        head = number

    role = _ROLE_BEFORE_NUMBER.search(head)
    if role:
        parsed.fields["role"] = role.group(1).upper()
        head = head[: role.start()].rstrip("-_ .")

    # Remaining dash tokens: volume, level and type read from the right, and
    # only when a token looks like a code rather than free text.
    remaining = [token for token in re.split(r"[-_ .]+", head) if token]
    for index, token in enumerate(reversed(remaining)):
        field_name = ("type", "level", "volume")[min(index, 2)]
        if re.fullmatch(r"[A-Za-z0-9]{1,6}", token) and parsed.get(field_name) is None:
            parsed.fields[field_name] = token.upper()
        else:
            break
    parsed.fields = {key: value for key, value in parsed.fields.items() if value}
    leftover = [token for token in remaining[: len(remaining) - len(parsed.fields)] if token]
    if not parsed.fields and not leftover:
        leftover = []
    parsed.leftover = leftover
    return parsed


# ── Resolving tokens from a sheet ───────────────────────────────────────


def _token_value(
    token: str,
    sheet: SheetRecord,
    parsed: ParsedNumber,
    context: dict[str, str],
    filename: str,
    extension: str,
) -> str | None:
    """Resolve one token, or None when this sheet cannot supply it."""
    value: str | None = None

    if token == "original":
        value = filename
    elif token == "project":
        value = context.get("project") or parsed.get("project")
    elif token == "originator":
        value = context.get("originator") or parsed.get("originator")
    elif token in {"volume", "level", "type", "role", "number"}:
        value = parsed.get(token) or context.get(token)
    elif token == "rev":
        value = sheet.revision or _rev_from_filename(filename)
    elif token == "title":
        value = sheet.title
    elif token == "date":
        value = _date_from_filename(filename)
    elif token == "status":
        value = _status_from_filename(filename) or context.get("status")
    elif token == "extension":
        value = extension

    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _rev_from_filename(filename: str) -> str | None:
    match = re.search(r"(?i)(?:rev(?:ision)?\.?\s*[-_:]?\s*)([a-z0-9]{1,4})", filename)
    return match.group(1).upper() if match else None


def _date_from_filename(filename: str) -> str | None:
    match = _DATE_IN_FILENAME.search(filename)
    return match.group(0).replace("/", "-") if match else None


def _status_from_filename(filename: str) -> str | None:
    lowered = filename.lower()
    for word, code in _STATUS_WORDS:
        if word in lowered:
            return code
    return None


def apply_modifier(value: str, modifier: str | None) -> str:
    """Token modifiers: ``{title:40}`` truncates, ``{rev:upper}`` uppercases,
    ``{number:pad4}`` zero-pads."""
    if not modifier:
        return value
    if modifier == "upper":
        return value.upper()
    if modifier == "lower":
        return value.lower()
    if modifier.startswith("pad") and modifier[3:].isdigit():
        return value.zfill(int(modifier[3:]))
    if modifier.isdigit():
        return value[: int(modifier)]
    return value


def render(
    template: str,
    sheet: SheetRecord,
    context: dict[str, str],
    *,
    parse_pattern: str | None = None,
) -> RenderResult:
    """Render *template* for one sheet into a sanitised file name."""
    filename = sheet.filename
    suffix = Path(filename).suffix or ""
    stem = filename[: -len(suffix)] if suffix else filename

    parsed = parse_drawing_number(sheet.drawing_no, parse_pattern)
    missing: list[str] = []
    used: dict[str, str] = {}
    output: list[str] = []
    position = 0

    for match in _TOKEN_PATTERN.finditer(template):
        output.append(template[position : match.start()])
        position = match.end()
        token, modifier = match.group(1), match.group(2)
        if token not in AVAILABLE_TOKENS and token != "extension":
            continue
        value = _token_value(token, sheet, parsed, context, stem, suffix.lstrip("."))
        if value is None:
            missing.append(token)
            continue
        applied = apply_modifier(value, modifier)
        if not applied:
            missing.append(token)
            continue
        output.append(applied)
        used[token] = applied
    output.append(template[position:])

    rendered = "".join(output)
    rendered = _collapse_double_separators(rendered).strip(" -_.")
    result = RenderResult(filename="", missing=missing, used=used)

    if not rendered:
        result.empty = True
        return result

    safe = sanitise_name(rendered)
    if not safe:
        result.empty = True
        return result
    result.filename = f"{safe}{suffix}"
    return result


def _collapse_double_separators(text: str) -> str:
    return re.sub(r"([-_ ])\1+", r"\1", text)


def sanitise_name(stem: str) -> str:
    """Make a stem safe for Windows: strip invalid characters, trailing dots
    and spaces. Reserved device names are detected separately, not renamed
    here, because they need a suffix the planner chooses."""
    cleaned = _INVALID_WINDOWS_CHARS.sub("", stem)
    return cleaned.strip().rstrip(". ")


def is_reserved_name(stem: str) -> bool:
    """True for CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without a dot."""
    return _RESERVED_DEVICES.fullmatch(stem.strip()) is not None


# ── Preset templates ─────────────────────────────────────────────────────


def naming_template_dirs() -> list[Path]:
    """Shipped templates first, then the user's own (survive an update)."""
    return [bundle_root() / "naming_templates", get_app_paths().naming_templates]


def available_templates() -> list[NamingTemplate]:
    found: dict[str, NamingTemplate] = {}
    for directory in naming_template_dirs():
        for candidate in sorted(directory.glob("*.json")):
            if candidate.stem.startswith("_"):
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            template_id = str(data.get("id", candidate.stem))
            found[template_id] = NamingTemplate(
                id=template_id,
                label=str(data.get("label", template_id)),
                template=str(data.get("template", "")),
                description=str(data.get("description", "")),
            )
    return list(found.values())


def load_template(template_id: str) -> NamingTemplate | None:
    for directory in naming_template_dirs():
        candidate = directory / f"{template_id}.json"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return NamingTemplate(
                id=str(data.get("id", candidate.stem)),
                label=str(data.get("label", candidate.stem)),
                template=str(data.get("template", "")),
                description=str(data.get("description", "")),
            )
        except (OSError, json.JSONDecodeError):
            continue
    return None


def save_template(template: NamingTemplate) -> Path:
    """Persist a (possibly custom) template to the user's app data."""
    target = get_app_paths().naming_templates / f"{template.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(template.as_dict(), indent=2), encoding="utf-8")
    return target


def preview(
    template: str,
    sheets: list[SheetRecord],
    context: dict[str, str],
    n: int = 3,
) -> list[str]:
    """Three real example file names for the live template preview."""
    rendered: list[str] = []
    for sheet in sheets:
        result = render(template, sheet, context)
        if not result.empty and result.filename not in rendered:
            rendered.append(result.filename)
        if len(rendered) >= n:
            break
    return rendered
