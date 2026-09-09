"""The pattern library: what a drawing number looks like on this project.

Every office numbers drawings differently, so the patterns live in a profile
JSON file rather than in the code. `profiles/default.json` ships with patterns
common across the industry; a project can add its own without a new release.

No AI here. Regex and geometry only, because a drawing number that changes
between two runs of the same file is worse than no drawing number at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from engine.storage.paths import bundle_root, get_app_paths

DEFAULT_PROFILE_ID = "default"

#: Labels that sit next to a drawing number in a title block.
DEFAULT_NUMBER_LABELS: tuple[str, ...] = (
    "drawing no",
    "drawing number",
    "dwg no",
    "dwg number",
    "drg no",
    "sheet no",
    "sheet number",
    "document no",
    "document number",
    "doc no",
    "ref no",
    "ref",
)

DEFAULT_REVISION_LABELS: tuple[str, ...] = ("rev", "revision", "rev no", "issue")

DEFAULT_TITLE_LABELS: tuple[str, ...] = (
    "drawing title",
    "sheet name",
    "sheet title",
    "title",
    "description",
)

DEFAULT_SCALE_LABELS: tuple[str, ...] = ("scale", "scales")

#: Drawing-number shapes seen across real projects, most specific first.
DEFAULT_NUMBER_PATTERNS: tuple[str, ...] = (
    # UVU-KEO-ARC-L03-DR-A-001234
    r"\b[A-Z]{2,5}(?:-[A-Z0-9]{1,6}){3,7}\b",
    # A-101, SK-12, ME-2001, G-000
    r"\b[A-Z]{1,3}-\d{2,5}[A-Z]?\b",
    # A101 / AR1001 without a separator
    r"\b[A-Z]{1,3}\d{3,5}[A-Z]?\b",
    # 1234-A-101
    r"\b\d{3,6}-[A-Z]{1,3}-\d{2,4}\b",
)

DEFAULT_REVISION_PATTERNS: tuple[str, ...] = (
    r"\b(?:REV(?:ISION)?\.?\s*[:\-]?\s*)([A-Z]{1,2}\d{0,2})\b",
    r"\b([PCTS]\d{2})\b",  # P01, C01, T02, S01
    r"\b(?:REV(?:ISION)?\.?\s*[:\-]?\s*)(\d{1,2})\b",
)

#: Words a drawing number must never be. These match the number shapes but
#: are drawing content, not identity.
NUMBER_STOPWORDS: frozenset[str] = frozenset(
    {
        "REV",
        "REVISION",
        "SCALE",
        "SHEET",
        "DATE",
        "NTS",
        "SIM",
        "TYP",
        "GRID",
        "LEVEL",
        "DETAIL",
        "SECTION",
        "NORTH",
        "SOUTH",
        "EAST",
        "WEST",
        "PLAN",
        "NOTE",
        "NOTES",
        "A1",
        "A0",
        "A2",
        "A3",
        "A4",
    }
)


@dataclass(slots=True)
class SheetProfile:
    """One client's drawing conventions."""

    id: str = DEFAULT_PROFILE_ID
    label: str = "Default"
    number_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_NUMBER_PATTERNS))
    number_labels: list[str] = field(default_factory=lambda: list(DEFAULT_NUMBER_LABELS))
    revision_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_REVISION_PATTERNS))
    revision_labels: list[str] = field(default_factory=lambda: list(DEFAULT_REVISION_LABELS))
    title_labels: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_LABELS))
    scale_labels: list[str] = field(default_factory=lambda: list(DEFAULT_SCALE_LABELS))
    #: Which revision scheme this client uses, when it is known in advance.
    revision_scheme: str = "unknown"
    #: Remembered column mapping for this client's drawing list.
    list_column_mapping: dict[str, str] = field(default_factory=dict)
    #: Fixed values for rename templates: project, originator, and a default
    #: status, resolved as `{project}`, `{originator}` and `{status}` tokens.
    naming: dict[str, str] = field(default_factory=dict)

    def compiled_number_patterns(self) -> list[re.Pattern[str]]:
        return _compile_all(tuple(self.number_patterns))

    def compiled_revision_patterns(self) -> list[re.Pattern[str]]:
        return _compile_all(tuple(self.revision_patterns))

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "number_patterns": self.number_patterns,
            "number_labels": self.number_labels,
            "revision_patterns": self.revision_patterns,
            "revision_labels": self.revision_labels,
            "title_labels": self.title_labels,
            "scale_labels": self.scale_labels,
            "revision_scheme": self.revision_scheme,
            "list_column_mapping": self.list_column_mapping,
            "naming": self.naming,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SheetProfile:
        base = cls()
        return cls(
            id=str(data.get("id", base.id)),
            label=str(data.get("label", base.label)),
            number_patterns=list(data.get("number_patterns", base.number_patterns)),  # type: ignore[arg-type]
            number_labels=list(data.get("number_labels", base.number_labels)),  # type: ignore[arg-type]
            revision_patterns=list(data.get("revision_patterns", base.revision_patterns)),  # type: ignore[arg-type]
            revision_labels=list(data.get("revision_labels", base.revision_labels)),  # type: ignore[arg-type]
            title_labels=list(data.get("title_labels", base.title_labels)),  # type: ignore[arg-type]
            scale_labels=list(data.get("scale_labels", base.scale_labels)),  # type: ignore[arg-type]
            revision_scheme=str(data.get("revision_scheme", base.revision_scheme)),
            list_column_mapping=dict(data.get("list_column_mapping", {})),  # type: ignore[arg-type]
            naming=dict(data.get("naming", {})),  # type: ignore[arg-type]
        )


@lru_cache(maxsize=32)
def _compile_all(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    """Compile a pattern tuple once. A bad pattern is skipped, not fatal."""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            logger.warning("Ignoring invalid pattern {!r}: {}", pattern, exc)
    return compiled


def is_plausible_number(candidate: str) -> bool:
    """Reject matches that are drawing content rather than an identity."""
    text = candidate.strip().upper()
    if len(text) < 3 or len(text) > 60:
        return False
    if text in NUMBER_STOPWORDS:
        return False
    # A number must contain at least one digit and one letter, or be clearly
    # structured. "3000" is a dimension; "A-101" is a drawing.
    has_digit = any(character.isdigit() for character in text)
    has_alpha = any(character.isalpha() for character in text)
    return has_digit and has_alpha


#: Words that match a revision shape but are never a revision code.
REVISION_STOPWORDS: frozenset[str] = frozenset(
    {"URGENT", "ISSUE", "DATE", "BY", "OF", "NO", "TO", "AS", "IN", "ON", "NTS"}
)


def is_plausible_revision(candidate: str) -> bool:
    """A revision is short and structured: A, B, P01, C02, 01, T3.

    Rejecting the near-misses matters because a wrong revision makes a sheet
    look revised when it was not, and that is a false positive the user pays
    for by checking a drawing that never changed.
    """
    text = candidate.strip().upper().strip(".:-")
    if not text or len(text) > 4:
        return False
    if text in REVISION_STOPWORDS:
        return False
    # Letters then optional digits (A, P01, C02), or digits alone (00, 01).
    return bool(re.fullmatch(r"[A-Z]{1,2}\d{0,2}|\d{1,2}", text))


def find_numbers(text: str, profile: SheetProfile | None = None) -> list[str]:
    """Every plausible drawing number in *text*, most specific pattern first."""
    profile = profile or load_profile()
    found: list[str] = []
    seen: set[str] = set()

    for pattern in profile.compiled_number_patterns():
        for match in pattern.finditer(text):
            candidate = match.group(0).strip().upper()
            if candidate in seen or not is_plausible_number(candidate):
                continue
            seen.add(candidate)
            found.append(candidate)
    return found


def find_revision(text: str, profile: SheetProfile | None = None) -> str | None:
    """The first revision code in *text*, or None."""
    profile = profile or load_profile()
    for pattern in profile.compiled_revision_patterns():
        for match in pattern.finditer(text):
            group = match.group(1) if match.groups() else match.group(0)
            candidate = group.strip().upper()
            if is_plausible_revision(candidate):
                return candidate
    return None


# ── Profile loading ────────────────────────────────────────────────────


def profile_search_paths() -> list[Path]:
    """Where profiles are looked for: shipped first, then user-saved."""
    return [bundle_root() / "profiles", get_app_paths().profiles]


def load_profile(profile_id: str = DEFAULT_PROFILE_ID) -> SheetProfile:
    """Load a profile by id, falling back to built-in defaults."""
    for directory in profile_search_paths():
        candidate = directory / f"{profile_id}.json"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            logger.debug("Loaded sheet profile {} from {}", profile_id, candidate)
            return SheetProfile.from_dict(data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read profile {}: {}", candidate, exc)

    if profile_id != DEFAULT_PROFILE_ID:
        logger.info("No profile '{}' found; using the default patterns", profile_id)
    return SheetProfile()


def available_profiles() -> list[dict[str, str]]:
    """Every profile that can be chosen, for the Setup screen dropdown."""
    found: dict[str, dict[str, str]] = {}
    for directory in profile_search_paths():
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.json")):
            if candidate.stem.startswith("_"):
                continue  # _schema.json and friends
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            profile_id = str(data.get("id", candidate.stem))
            found[profile_id] = {"id": profile_id, "label": str(data.get("label", profile_id))}

    if DEFAULT_PROFILE_ID not in found:
        found[DEFAULT_PROFILE_ID] = {"id": DEFAULT_PROFILE_ID, "label": "Default"}
    return list(found.values())


def save_profile(profile: SheetProfile) -> Path:
    """Write a profile into the user's app data, so it survives an update."""
    target = get_app_paths().profiles / f"{profile.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile.as_dict(), indent=2), encoding="utf-8")
    logger.info("Saved sheet profile {} to {}", profile.id, target)
    return target
