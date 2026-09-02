"""Read the sheet's own identity: number, title, revision, scale, size.

This is the most important piece of logic in Phase 2. Everything downstream —
matching, reconciliation, the register — is built on the drawing number, so
**how** the number was found is recorded alongside it. A quantity surveyor
will trust a number that came out of the title block and will check one that
came from a filename, and they can only do that if we tell them.

Priority order, from the plan:

    1. title block zone   high confidence    source = titleblock
    2. anywhere on sheet   medium            source = sheet_text
    3. the filename        medium            source = filename
    4. the drawing list    medium            source = drawing_list  (Task 2.6)
    5. the user typed it   certain           source = user
    6. a vision model      medium            source = ai            (Phase 7)

Label-to-value geometry is the fiddly part, and real title blocks do it two
different ways in the same block:

    Drawing No.          <- label
    A-102                <- value directly BELOW

    Scale   1 : 100      <- value to the RIGHT of the label

So both directions are searched and the nearest sensible candidate wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from engine.extract.text_extractor import PageText, TextItem
from engine.titleblock.patterns import (
    SheetProfile,
    find_numbers,
    find_revision,
    is_plausible_number,
    is_plausible_revision,
    load_profile,
)
from engine.titleblock.scale_reader import ScaleReading, read_scale
from engine.titleblock.zone_detector import Zone, ZoneName, detect_zones


class NumberSource(StrEnum):
    """Where a drawing number came from. Always shown to the user."""

    TITLEBLOCK = "titleblock"
    SHEET_TEXT = "sheet_text"
    FILENAME = "filename"
    DRAWING_LIST = "drawing_list"
    USER = "user"
    AI = "ai"
    NONE = "none"


#: Confidence attached to each source, before any adjustment.
SOURCE_CONFIDENCE: dict[NumberSource, float] = {
    NumberSource.TITLEBLOCK: 0.95,
    NumberSource.SHEET_TEXT: 0.6,
    NumberSource.FILENAME: 0.55,
    NumberSource.DRAWING_LIST: 0.7,
    NumberSource.USER: 1.0,
    NumberSource.AI: 0.6,
    NumberSource.NONE: 0.0,
}

#: How the user sees each source explained.
SOURCE_EXPLANATION: dict[NumberSource, str] = {
    NumberSource.TITLEBLOCK: "Read from the title block on the sheet.",
    NumberSource.SHEET_TEXT: "Found in the sheet text, but not inside the title block.",
    NumberSource.FILENAME: "Taken from the file name. The sheet itself was not readable.",
    NumberSource.DRAWING_LIST: "Matched against the imported drawing list.",
    NumberSource.USER: "You entered this.",
    NumberSource.AI: "Read by the vision model.",
    NumberSource.NONE: "No drawing number could be found.",
}

#: How far below a label its value may sit, as a fraction of page height.
BELOW_BAND = 0.06
#: How far right of a label its value may sit, as a fraction of page width.
RIGHT_BAND = 0.18
#: A value below a label must be roughly in the same column.
COLUMN_BAND = 0.08
#: How hard to punish a candidate that is off to one side of its label.
#: Above 1.0 so being in the right column beats being marginally closer.
OFF_AXIS_PENALTY = 4.0

_SEPARATORS = re.compile(r"[\s_]+")
#: A trailing revision marker in a file name: `-Rev-D`, `-RevD`, `-REV01`,
#: `(Rev C)`. No word boundary after REV, because `RevD` is written solid.
_REVISION_SUFFIX = re.compile(r"[-\s(\[]*\bREV(?:ISION)?[-.\s:]*[A-Z]{0,2}\d{0,2}[)\]]*.*$")

#: A wrapped title line must start at the same left edge, to this tolerance.
WRAP_COLUMN_TOLERANCE = 0.006
#: ...and sit within this multiple of its own line height below.
WRAP_LINE_GAP_RATIO = 1.8
#: ...and be set at a similar size, which is what separates a second title
#: line from the next label down.
WRAP_HEIGHT_TOLERANCE = 0.3
#: Titles never run past this many lines in a title block cell.
WRAP_MAX_LINES = 2


@dataclass(slots=True)
class FieldResult:
    """One extracted field, with where it came from and how sure we are."""

    value: str | None = None
    source: NumberSource = NumberSource.NONE
    confidence: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.value)


@dataclass(slots=True)
class SheetIdentity:
    """Everything read from one sheet's title block."""

    page_index: int = 0
    drawing_no: str | None = None
    source_of_number: NumberSource = NumberSource.NONE
    number_confidence: float = 0.0
    title: str | None = None
    revision: str | None = None
    scale: str | None = None
    sheet_size: str | None = None

    #: The number as read from the file name, kept even when the title block won.
    filename_number: str | None = None
    #: True when the title block and the file name disagree. Document
    #: controllers check this first, so it is surfaced prominently.
    number_mismatch: bool = False

    warnings: list[str] = field(default_factory=list)

    @property
    def identified(self) -> bool:
        return bool(self.drawing_no)

    @property
    def source_explanation(self) -> str:
        return SOURCE_EXPLANATION[self.source_of_number]

    def as_dict(self) -> dict[str, object]:
        return {
            "page_index": self.page_index,
            "drawing_no": self.drawing_no,
            "source_of_number": str(self.source_of_number),
            "number_confidence": round(self.number_confidence, 3),
            "title": self.title,
            "revision": self.revision,
            "scale": self.scale,
            "sheet_size": self.sheet_size,
            "filename_number": self.filename_number,
            "number_mismatch": self.number_mismatch,
            "warnings": list(self.warnings),
        }


# ── Label geometry ─────────────────────────────────────────────────────


def find_label(items: list[TextItem], labels: list[str] | tuple[str, ...]) -> TextItem | None:
    """The text item that is one of *labels*.

    Two rules, both learned from real sheets:

    * **Label order is priority order.** The lists are written most specific
      first, so "drawing title" beats "description". Without this, a materials
      legend headed "DESCRIPTION" near the top of the sheet gets mistaken for
      the title block's title cell.
    * **When a label appears twice, take the lowest one.** The title block sits
      at the bottom of the sheet, so the bottom-most match is the right one.

    A cell reading exactly "Drawing No." is a label. A paragraph that merely
    mentions the words is not, so long text is rejected.
    """
    candidates: list[tuple[str, bool, TextItem]] = []

    for item in items:
        cleaned = item.clean.lower().strip(" .:_-")
        if not cleaned or len(cleaned) > 40:
            continue
        for label in labels:
            if cleaned == label:
                candidates.append((label, True, item))
            elif cleaned.startswith(label) and len(cleaned) <= len(label) + 4:
                candidates.append((label, False, item))

    if not candidates:
        return None

    order = {label: index for index, label in enumerate(labels)}
    # Priority order, then exact before partial, then the bottom-most match.
    candidates.sort(key=lambda entry: (order[entry[0]], not entry[1], entry[2].fy))
    return candidates[0][2]


def value_near_label(
    label: TextItem,
    items: list[TextItem],
    *,
    accept: object = None,
) -> TextItem | None:
    """The value belonging to *label*: directly below it, or to its right.

    Real title blocks use both arrangements in the same block, so both are
    searched and the nearest acceptable candidate wins. `accept` is an
    optional predicate to reject candidates that are not the right shape.
    """
    candidates: list[tuple[float, TextItem]] = []

    for item in items:
        if item is label or not item.clean:
            continue
        if accept is not None and not accept(item.clean):  # type: ignore[operator]
            continue

        # Directly below, in roughly the same column.
        below = label.fy - item.fy
        column_offset = abs(item.fx - label.fx)
        if 0 < below <= BELOW_BAND and column_offset <= COLUMN_BAND:
            # Weight the sideways offset heavily. Title block cells sit on a
            # shared baseline, so several values are the same distance below
            # their labels and only the column tells them apart. Without this
            # the value under the neighbouring cell wins on a tie -- which is
            # how "Project No." gets read as the drawing number.
            candidates.append((below + column_offset * OFF_AXIS_PENALTY, item))
            continue

        # To the right, on roughly the same line.
        across = item.fx - label.fx
        row_offset = abs(item.fy - label.fy)
        if 0 < across <= RIGHT_BAND and row_offset <= BELOW_BAND / 3:
            candidates.append((across + row_offset * OFF_AXIS_PENALTY, item))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


# ── Filename ───────────────────────────────────────────────────────────


def number_from_filename(filename: str, profile: SheetProfile | None = None) -> str | None:
    """The drawing number encoded in a file name, if there is one.

    The revision suffix is cut off first. Without that, a long structured
    number runs straight on into it and `UVU-KEO-ARC-L03-DR-A-001234-Rev-D`
    yields `UVU-KEO-ARC-L03-DR-A-001234-REV` — which is not the drawing
    number, and worse, differs from the same file named `...-RevD`.
    """
    stem = Path(filename).stem
    # Separators vary; normalise so `A_101` and `A 101` read like `A-101`.
    normalised = _SEPARATORS.sub("-", stem).upper()
    normalised = _REVISION_SUFFIX.sub("", normalised)

    numbers = find_numbers(normalised, profile)
    return numbers[0] if numbers else None


def collect_wrapped_value(first: TextItem, items: list[TextItem]) -> str:
    """Join a value with its continuation lines.

    A long drawing title wraps inside its cell, so "GROUND FLOOR REFLECTED
    CEILING" and "PLAN" are two separate lines. A continuation line starts at
    the same left edge, sits just below, and is set at the same size — that
    last test is what stops the next label ("Drawn", in a smaller font) being
    swallowed into the title.
    """
    parts = [first.clean]
    current = first
    seen = {id(first)}

    for _ in range(WRAP_MAX_LINES - 1):
        if current.fh <= 0:
            break

        best: TextItem | None = None
        best_gap = float("inf")

        for item in items:
            if id(item) in seen or not item.clean:
                continue
            gap = current.fy - item.fy
            if gap <= 0 or gap > current.fh * WRAP_LINE_GAP_RATIO:
                continue
            if abs(item.fx - current.fx) > WRAP_COLUMN_TOLERANCE:
                continue
            if abs(item.fh - current.fh) > current.fh * WRAP_HEIGHT_TOLERANCE:
                continue
            if not is_plausible_title(item.clean):
                continue
            if gap < best_gap:
                best, best_gap = item, gap

        if best is None:
            break
        parts.append(best.clean)
        seen.add(id(best))
        current = best

    return " ".join(parts)


def is_plausible_title(candidate: str) -> bool:
    """A drawing title, as opposed to a general note picked up nearby.

    Titles are short and are not sentences. A general note such as
    "12mm THK. Gypsum Board." sits close to the title cell on some sheets, so
    anything ending in a full stop is rejected.
    """
    text = candidate.strip()
    if len(text) < 3 or len(text) > 80:
        return False
    if text.endswith("."):
        return False
    # A title has letters in it, and is not just a number or a date.
    return any(character.isalpha() for character in text)


def normalise_number(number: str | None) -> str:
    """Comparison form: upper case, no spaces, underscores or separators.

    Used for matching, never for display. `A-101`, `A 101` and `a_101` all
    normalise to `A101`.
    """
    if not number:
        return ""
    return re.sub(r"[^A-Z0-9]", "", number.upper())


# ── The extractor ──────────────────────────────────────────────────────


def _extract_from_zone(zone: Zone, profile: SheetProfile) -> FieldResult:
    """Try to read a drawing number out of one zone."""
    label = find_label(zone.items, profile.number_labels)
    if label is not None:
        value = value_near_label(label, zone.items, accept=is_plausible_number)
        if value is not None:
            numbers = find_numbers(value.clean, profile)
            candidate = numbers[0] if numbers else value.clean.strip().upper()
            if is_plausible_number(candidate):
                return FieldResult(candidate, NumberSource.TITLEBLOCK, 0.95)

    # No usable label: fall back to a pattern sweep inside the zone.
    numbers = find_numbers(zone.text(), profile)
    if numbers:
        # Lower confidence: matched by shape alone, with no label to confirm it.
        return FieldResult(numbers[0], NumberSource.TITLEBLOCK, 0.75)

    return FieldResult()


def extract_identity(
    page: PageText,
    filename: str = "",
    profile: SheetProfile | None = None,
    sheet_size: str | None = None,
    *,
    filename_names_the_sheet: bool = True,
) -> SheetIdentity:
    """Read the identity of one sheet.

    Reads the number from the title block **and** from the file name, so a
    disagreement between them can be flagged.

    `filename_names_the_sheet` must be False when the file holds more than one
    drawing. A 30-drawing issue PDF is named for the issue, not for any sheet
    inside it, so comparing the two is meaningless: it flagged every sheet in a
    real issue as a mismatch and buried the findings that mattered.
    """
    profile = profile or load_profile()
    identity = SheetIdentity(page_index=page.page_index, sheet_size=sheet_size)

    identity.filename_number = (
        number_from_filename(filename, profile) if filename and filename_names_the_sheet else None
    )

    zones = [zone for zone in detect_zones(page) if not zone.is_empty]
    title_zones = [zone for zone in zones if zone.name is not ZoneName.WHOLE_SHEET]

    # 1. The title block.
    best = FieldResult()
    for zone in title_zones:
        result = _extract_from_zone(zone, profile)
        if result.found and result.confidence > best.confidence:
            best = result
        if best.confidence >= 0.95:
            break

    # 2. Anywhere on the sheet.
    if not best.found:
        numbers = find_numbers(page.joined(), profile)
        if numbers:
            best = FieldResult(numbers[0], NumberSource.SHEET_TEXT, 0.6)

    # 3. The file name.
    if not best.found and identity.filename_number:
        best = FieldResult(identity.filename_number, NumberSource.FILENAME, 0.55)

    identity.drawing_no = best.value
    identity.source_of_number = best.source
    identity.number_confidence = best.confidence

    # ⚠ The mismatch flag. Extremely common on real projects, and the first
    # thing a document controller checks by hand.
    if (
        identity.drawing_no
        and identity.filename_number
        and best.source is not NumberSource.FILENAME
        and normalise_number(identity.drawing_no) != normalise_number(identity.filename_number)
    ):
        identity.number_mismatch = True
        identity.warnings.append(
            f"The title block says {identity.drawing_no} but the file is named "
            f"{identity.filename_number}. Check which is correct."
        )

    _extract_other_fields(page, title_zones, profile, identity, filename)

    if not identity.identified:
        identity.warnings.append(
            "No drawing number could be found on this sheet. Open it and enter "
            "the number, or choose a different sheet profile."
        )

    return identity


def _extract_other_fields(
    page: PageText,
    title_zones: list[Zone],
    profile: SheetProfile,
    identity: SheetIdentity,
    filename: str,
) -> None:
    """Title, revision and scale. Each is optional and never guessed wildly."""
    items: list[TextItem] = []
    for zone in title_zones:
        items.extend(zone.items)

    # Title: the text under the "Drawing Title" label.
    title_label = find_label(items, profile.title_labels)
    if title_label is not None:
        value = value_near_label(title_label, items, accept=is_plausible_title)
        if value is not None:
            identity.title = collect_wrapped_value(value, items)

    # Revision: the value beside the "Rev." label, else a pattern sweep.
    revision_label = find_label(items, profile.revision_labels)
    if revision_label is not None:
        value = value_near_label(revision_label, items, accept=is_plausible_revision)
        if value is not None and value.clean.strip():
            identity.revision = value.clean.strip().upper()

    if not identity.revision:
        zone_text = "\n".join(zone.text() for zone in title_zones)
        identity.revision = find_revision(zone_text, profile)

    if not identity.revision and filename:
        identity.revision = find_revision(Path(filename).stem.replace("_", " "), profile)

    # Scale.
    scale_label = find_label(items, profile.scale_labels)
    reading: ScaleReading | None = None
    if scale_label is not None:
        value = value_near_label(scale_label, items)
        if value is not None:
            reading = read_scale(value.clean)

    if reading is None or not reading.is_known:
        reading = read_scale("\n".join(zone.text() for zone in title_zones))

    if reading.is_known:
        identity.scale = reading.text
