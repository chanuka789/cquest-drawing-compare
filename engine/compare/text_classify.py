"""Task 5.5 — what a piece of text on a drawing actually is.

`3000` and `A` and `Provide 100mm blockwork throughout` are all text, and a
comparison that treats them the same produces a report nobody can read. The
category decides two things: how a change is described in words, and how much
it is likely to cost. `3000` becoming `3200` is a wall moving. `A` becoming
`B` is a grid letter, which almost never costs anything.

No AI is involved and none is needed — this is patterns, plus a little
geometry context when it is available. Every rule is configurable through
:class:`ClassifyConfig` so a practice with its own tagging convention can
teach the application without a code change.

Classification never *fails*: `unknown` is a real answer and is treated as
one. A wrong confident category is worse than an honest shrug, because the
report sorts by category and a note filed as a dimension gets a numeric
analysis it has no business having.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from engine.compare.types import Bbox, TextCategory

# ── Patterns ────────────────────────────────────────────────────────────

#: A number, optionally with thousands separators and a decimal part.
_NUMBER = r"[-+±]?\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|[-+±]?\d+(?:\.\d+)?"

NUMBER_PATTERN = re.compile(rf"^(?P<value>{_NUMBER})$")
NUMBER_WITH_UNIT = re.compile(rf"^(?P<value>{_NUMBER})\s*(?P<unit>MM|M|CM|KM|MM\.|M\.)$", re.I)
#: Two or more values separated by a slash: a dimension string.
DIMENSION_STRING = re.compile(rf"^(?:{_NUMBER})(?:\s*/\s*(?:{_NUMBER}))+$")
#: A range: 3000-3200, 3000 to 3200. Drawing offices type an en dash as often
#: as a hyphen, so both separators are accepted.
RANGE_PATTERN = re.compile(rf"^(?P<low>{_NUMBER})\s*(?:-|–|TO)\s*(?P<high>{_NUMBER})$", re.I)

LEVEL_PATTERN = re.compile(r"^[+\-±]?\d+\.\d{2,3}$")
LEVEL_PREFIXES = ("FFL", "SSL", "TOS", "SOF", "IL", "CL", "TOC", "FGL", "TBC LEVEL")

SCALE_PATTERN = re.compile(r"^1\s*[:/]\s*\d+$")
SCALE_WORDS = ("NTS", "N.T.S.", "AS SHOWN", "AS NOTED", "NOT TO SCALE", "VARIOUS")

#: A tag is short and alphanumeric: D-12, W3, C1, SK-04.
TAG_PATTERN = re.compile(r"^[A-Z]{1,3}[-/ ]?\d{1,3}[A-Z]?$")
#: Extra tag shapes a practice can add in the profile.
DEFAULT_TAG_PATTERNS: tuple[str, ...] = (r"^D-?\d+$", r"^W\d+$", r"^P\d{1,2}$", r"^FD-?\d+$")

GRID_PATTERN = re.compile(r"^[A-Z]{1,2}$|^\d{1,2}$")

DEFAULT_SPEC_KEYWORDS: tuple[str, ...] = (
    "FIRE",
    "RATED",
    "HR",
    "HOUR",
    "WATERPROOF",
    "TANKING",
    "ACOUSTIC",
    "MIN",
    "MAX",
    "THK",
    "THICK",
    "GRADE",
    "CLASS",
    "GALV",
    "INSULATION",
    "U-VALUE",
    "DPC",
    "DPM",
)

AREA_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:M2|M²|SQ\.?\s*M)", re.I)

#: More words than this and it is prose, not a label.
NOTE_WORD_COUNT = 4
#: Tags are short by definition.
MAX_TAG_CHARACTERS = 6


def normalise(text: str) -> str:
    """Comparison form: NFKC, collapsed whitespace, upper case."""
    return " ".join(unicodedata.normalize("NFKC", text).upper().split())


# ── Numbers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParsedNumber:
    """A number read off a drawing, with whatever unit it carried."""

    value: float
    unit: str = ""
    raw: str = ""
    #: "single", "range", "string" (3000/2400) or "level".
    kind: str = "single"
    values: tuple[float, ...] = ()

    @property
    def in_millimetres(self) -> float:
        """The value in millimetres, using the unit written on the drawing.

        A bare number on a construction drawing is millimetres by convention;
        a level is written in metres. The unit is never guessed beyond that.
        """
        factors = {"": 1.0, "MM": 1.0, "CM": 10.0, "M": 1000.0, "KM": 1_000_000.0}
        return self.value * factors.get(self.unit.upper().rstrip("."), 1.0)


def _to_float(text: str) -> float | None:
    cleaned = text.replace(" ", "").replace(",", "").replace("±", "")
    if cleaned in {"", "+", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_numeric(text: str) -> ParsedNumber | None:
    """Extract a value and a unit, handling the ways drawings write numbers.

    Handles ``3000``, ``3,000``, ``2400mm``, ``2.4m``, ``+3.600``,
    ``3000/2400`` (a dimension string — the first value leads) and
    ``3000-3200`` (a range — the midpoint leads, both values kept).
    Returns None for anything that is not a number, which is most text.
    """
    cleaned = normalise(text)
    if not cleaned:
        return None

    match = NUMBER_WITH_UNIT.match(cleaned)
    if match:
        value = _to_float(match.group("value"))
        if value is not None:
            return ParsedNumber(value=value, unit=match.group("unit"), raw=text, values=(value,))

    match = NUMBER_PATTERN.match(cleaned)
    if match:
        value = _to_float(match.group("value"))
        if value is not None:
            kind = "level" if LEVEL_PATTERN.match(cleaned) else "single"
            return ParsedNumber(value=value, unit="", raw=text, kind=kind, values=(value,))

    match = RANGE_PATTERN.match(cleaned)
    if match:
        low = _to_float(match.group("low"))
        high = _to_float(match.group("high"))
        if low is not None and high is not None:
            return ParsedNumber(
                value=(low + high) / 2.0, unit="", raw=text, kind="range", values=(low, high)
            )

    if DIMENSION_STRING.match(cleaned):
        parts = [_to_float(part) for part in re.split(r"\s*/\s*", cleaned)]
        values = tuple(part for part in parts if part is not None)
        if values:
            return ParsedNumber(value=values[0], unit="", raw=text, kind="string", values=values)

    return None


# ── Configuration and context ───────────────────────────────────────────


@dataclass(slots=True)
class ClassifyConfig:
    """Every rule, so a practice can tune them without touching the code."""

    tag_patterns: tuple[str, ...] = DEFAULT_TAG_PATTERNS
    spec_keywords: tuple[str, ...] = DEFAULT_SPEC_KEYWORDS
    level_prefixes: tuple[str, ...] = LEVEL_PREFIXES
    scale_words: tuple[str, ...] = SCALE_WORDS
    note_word_count: int = NOTE_WORD_COUNT
    max_tag_characters: int = MAX_TAG_CHARACTERS
    #: How close to a dimension line a number must sit, in paper millimetres.
    dimension_line_paper_mm: float = 8.0


@dataclass(slots=True)
class ClassifyContext:
    """What the sheet around one text item looks like.

    Every field is optional. With none of them the classifier still works on
    patterns alone; with them it can tell a dimension from a door number and
    a grid letter from a tag, which patterns alone never can.
    """

    #: Bounding boxes of detected dimension lines, in image pixels.
    dimension_lines: list[Bbox] = field(default_factory=list)
    #: Circles, hexagons and rectangles that enclose text (grid bubbles).
    enclosures: list[Bbox] = field(default_factory=list)
    #: Closed regions that make a room: text inside one is a room name.
    room_regions: list[Bbox] = field(default_factory=list)
    #: Boxes of area figures (`24.5 m²`), which sit beside room names.
    area_labels: list[Bbox] = field(default_factory=list)
    #: Median text height in pixels, for relative-size rules.
    median_height_px: float = 0.0
    px_per_mm: float = 200 / 25.4
    #: True when the item falls inside a masked zone.
    masked: bool = False

    def near_dimension_line(self, box: Bbox, paper_mm: float) -> bool:
        limit = paper_mm * self.px_per_mm
        grown = box.expanded(limit)
        return any(grown.intersects(line) for line in self.dimension_lines)

    def enclosed_by(self, box: Bbox) -> Bbox | None:
        for enclosure in self.enclosures:
            if enclosure.intersection_area(box) > box.area * 0.6:
                return enclosure
        return None

    def inside_room(self, box: Bbox) -> bool:
        return any(region.intersection_area(box) > box.area * 0.9 for region in self.room_regions)

    def has_area_label_near(self, box: Bbox, paper_mm: float = 25.0) -> bool:
        grown = box.expanded(paper_mm * self.px_per_mm)
        return any(grown.intersects(label) for label in self.area_labels)


@dataclass(frozen=True, slots=True)
class Classification:
    """A category, how sure we are, and why."""

    category: TextCategory
    confidence: float
    evidence: str = ""
    number: ParsedNumber | None = None


# ── The classifier ──────────────────────────────────────────────────────


def classify(
    text: str,
    box: Bbox | None = None,
    context: ClassifyContext | None = None,
    config: ClassifyConfig | None = None,
) -> Classification:
    """Put one piece of drawing text into a category.

    Order matters and is deliberate. A masked item is title block before
    anything else. A scale reads as a scale even though `1:100` also looks
    like a number. A grid bubble beats a tag, because `A` inside a circle is
    a grid line and `A` on its own is nothing useful.
    """
    config = config or ClassifyConfig()
    context = context or ClassifyContext()
    box = box or Bbox(0.0, 0.0, 0.0, 0.0)
    cleaned = " ".join(text.split())
    upper = normalise(text)

    if context.masked:
        return Classification(TextCategory.TITLEBLOCK, 1.0, "Inside a masked title block zone.")

    if not upper:
        return Classification(TextCategory.UNKNOWN, 0.2, "Empty after normalisation.")

    # Scale, before numbers: `1:100` would otherwise read as a dimension.
    if SCALE_PATTERN.match(upper) or upper in config.scale_words:
        return Classification(TextCategory.SCALE, 0.95, "Reads as a drawing scale.")

    level = _level_of(upper, config)
    if level is not None:
        return level

    words = upper.split()
    enclosure = context.enclosed_by(box)
    if enclosure is not None and GRID_PATTERN.match(upper):
        return Classification(
            TextCategory.GRID, 0.9, "A single letter or number inside a grid bubble."
        )

    tag = _tag_of(upper, config, enclosure is not None)
    if tag is not None:
        return tag

    number = parse_numeric(cleaned)
    if number is not None:
        near_line = context.near_dimension_line(box, config.dimension_line_paper_mm)
        if near_line:
            return Classification(
                TextCategory.DIMENSION,
                0.95,
                "A number sitting on a dimension line.",
                number,
            )
        # Without geometry to lean on, a bare number on a drawing is still
        # almost always a dimension — but say so with less confidence.
        confidence = 0.7 if number.kind in {"single", "string", "range"} else 0.6
        return Classification(
            TextCategory.DIMENSION, confidence, "A number, with no dimension line nearby.", number
        )

    if len(words) > config.note_word_count:
        if _has_spec_keyword(upper, config):
            return Classification(
                TextCategory.SPEC, 0.85, "A sentence carrying specification wording."
            )
        return Classification(TextCategory.NOTE, 0.9, "More than four words: prose, not a label.")

    if _has_spec_keyword(upper, config):
        return Classification(TextCategory.SPEC, 0.8, "Carries specification wording.")

    if context.inside_room(box):
        evidence = "Inside a closed region"
        if context.has_area_label_near(box):
            evidence += " with an area figure beside it"
        return Classification(TextCategory.ROOM, 0.85, evidence + ".")

    if GRID_PATTERN.match(upper) and len(upper) <= 2:
        return Classification(
            TextCategory.GRID, 0.5, "A lone letter or number, with no bubble detected."
        )

    if upper.isalpha() and 1 < len(words) <= config.note_word_count:
        return Classification(TextCategory.ROOM, 0.5, "A short phrase of words: probably a label.")

    return Classification(TextCategory.UNKNOWN, 0.3, "No rule matched.")


def _level_of(upper: str, config: ClassifyConfig) -> Classification | None:
    """Levels: `+3.600`, `FFL +0.150`, `SSL 24.250`."""
    if LEVEL_PATTERN.match(upper):
        return Classification(TextCategory.LEVEL, 0.9, "Reads as a level.", parse_numeric(upper))
    for prefix in config.level_prefixes:
        if upper.startswith(prefix):
            remainder = upper[len(prefix) :].strip(" :=")
            number = parse_numeric(remainder)
            return Classification(
                TextCategory.LEVEL,
                0.95 if number else 0.7,
                f"Prefixed '{prefix}', which marks a level.",
                number,
            )
    return None


def _tag_of(upper: str, config: ClassifyConfig, enclosed: bool) -> Classification | None:
    """Tags: short, alphanumeric, often in a box or a circle."""
    condensed = upper.replace(" ", "")
    if len(condensed) > config.max_tag_characters:
        return None
    for pattern in config.tag_patterns:
        if re.match(pattern, condensed):
            return Classification(TextCategory.TAG, 0.92, f"Matches the tag pattern {pattern}.")
    if TAG_PATTERN.match(condensed):
        confidence = 0.85 if enclosed else 0.75
        evidence = "Short alphanumeric tag"
        if enclosed:
            evidence += ", drawn inside a box or circle"
        return Classification(TextCategory.TAG, confidence, evidence + ".")
    return None


def _has_spec_keyword(upper: str, config: ClassifyConfig) -> bool:
    """Whole words only.

    A substring test looks harmless until `THROUGHOUT` matches `HR` and every
    general note on the sheet is filed as specification text. Multi-word
    keywords are matched as phrases, which is the only case where a substring
    test is the right one.
    """
    words = set(re.findall(r"[A-Z0-9\-]+", upper))
    for keyword in config.spec_keywords:
        if " " in keyword:
            if keyword in upper:
                return True
        elif keyword in words:
            return True
    return False


def median_height(boxes: list[Bbox]) -> float:
    """Median box height in pixels, for the relative-size rules."""
    heights = [box.h for box in boxes if box.h > 0]
    return float(np.median(heights)) if heights else 0.0
