"""Clean a file name or drawing number so that two spellings of the same thing
compare equal.

Phase 2 normalised drawing numbers by stripping everything that was not a
letter or digit. Phase 3 has to survive the whole mess that real issue folders
contain — separator swaps, case changes, revision suffixes, date stamps,
sequence prefixes, status words, Windows copy junk, Unicode lookalikes and
trailing dots — so it needs three levels of cleaning, each leaving less behind:

* ``LIGHT`` — NFKC, lowercase, lookalike characters mapped (en-dash to
  hyphen), whitespace collapsed, every separator unified to a single hyphen.
  Two files whose only difference is ``-`` vs ``_`` vs a space compare equal.
* ``MEDIUM`` — everything LIGHT does, plus the file extension, revision
  suffixes, date stamps and sequence prefixes are stripped **from the ends
  inward**. The middle of a name is never touched, so ``01`` inside
  ``UVU-ARC-001`` survives while a leading ``01 -`` does not.
* ``AGGRESSIVE`` — everything MEDIUM does, plus status words, copy junk,
  sheet size markers and every remaining separator disappear, leaving only
  alphanumerics. Two names that share no words but the same drawing number
  collapse to the same key.

The Unicode step comes first for a reason that once cost a real afternoon: an
en-dash looks identical to a hyphen on screen but is a different character, so
without NFKC + a lookalike map ``UVU-ARC-001`` and ``UVU--ARC--001`` are
"different" names and every matcher downstream chases a phantom bug.

The result records **what was stripped**, in plain English, because the
matcher has to be able to tell the user *why* two names were judged the same
("the date stamp ``12.04.2026`` and the revision suffix ``Rev D`` were
removed"). The strip word lists are deliberately conservative and live in one
place so a project whose "final" is a status code, not junk, can adjust them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

#: A supported drawing file extension, lower case, no dot.
FILE_EXTENSIONS: frozenset[str] = frozenset(
    {"pdf", "dwg", "dxf", "dgn", "tif", "tiff", "png", "jpg", "jpeg", "bmp"}
)

#: Unicode characters that look like an ASCII character but are not it.
#: NFKC handles most of these; this map catches the remainder NFKC keeps.
_LOOKALIKES: dict[str, str] = {
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2212": "-",  # minus sign
    "\u00a0": " ",  # non-breaking space
    "\u2007": " ",  # figure space
    "\u202f": " ",  # narrow no-break space
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
}

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

#: Date stamps in the shapes real issue folders use, matched at the very end
#: of the name: ``2026-04-12``, ``12.04.26``, ``20260412``, ``12-Apr-2026``,
#: ``Apr 2026``.
_DATE_SUFFIX = re.compile(
    r"(?:"
    r"\d{4}[-_./]\d{1,2}[-_./]\d{1,2}"  # 2026-04-12, 2026/04/12, 2026.04.12
    r"|\d{1,2}[-_./]\d{1,2}[-_./]\d{2,4}"  # 12.04.26, 12/04/2026
    r"|\d{8}"  # 20260412
    r"|\d{1,2}[-_ ]?(?:" + _MONTHS + r")[a-z]*[-_ ]?\d{2,4}"  # 12-Apr-2026
    r"|(?:" + _MONTHS + r")[a-z]*[-_ ]\d{4}"  # Apr 2026
    r")$",
    re.IGNORECASE,
)

#: A revision written against the name: ``RevD``, ``Rev D``, ``-RevD``,
#: ``REV01``, ``Rev-D``. The leading separator is deliberately NOT part of the
#: pattern: :func:`_strip_tail` checks that whatever precedes the match is a
#: boundary, so a suffix rule can never start in the middle of a word.
_REVISION_SUFFIX = re.compile(
    r"rev(?:ision)?[-_ .(]*(?P<code>\d{1,2}|[a-z]{1,2}\d{0,2})\)?$",
    re.IGNORECASE,
)

#: A revision dropped straight after an underscore, ``_D``, ``_P01``, ``_C02``.
#: Self-delimiting (starts with ``_``), so no outer boundary is required.
_UNDERSCORE_CODE = re.compile(r"_(?P<code>[a-z]{1,2}\d{0,2}|p\d{2}|c\d{2})$", re.IGNORECASE)

#: A parenthesised revision at the very end: ``(D)``, ``(C02)``. Needs a
#: letter first so Windows copy junk ``(1)`` is never mistaken for a revision.
_PAREN_REVISION = re.compile(r"\([a-z]{1,2}\d{0,2}\)$", re.IGNORECASE)

#: A sequence prefix at the very front: ``01_UVU...``, ``001 - UVU...``.
#: Limited to three digits so a real drawing number that starts with digits
#: (``1234-A-101``) is not mistaken for a prefix.
_SEQUENCE_PREFIX = re.compile(r"^\d{1,3}[-_ .]+")

#: Windows copy junk, in the shapes Windows actually writes it.
_COPY_PREFIX = re.compile(r"^copy\s+of\s+", re.IGNORECASE)
_COPY_SUFFIX = re.compile(r"[-_ ](?:copy|\(-?\d+\))$", re.IGNORECASE)

#: Words that only mark a sheet size: ``A0`` … ``A4`` as a whole token.
_SHEET_SIZE_TOKEN = re.compile(r"a[0-4]", re.IGNORECASE)

#: Junk words, lower case, stripped only from the two ends at AGGRESSIVE.
JUNK_WORDS: frozenset[str] = frozenset(
    {
        # status words
        "for",
        "approval",
        "construction",
        "ifc",
        "ifa",
        "ift",
        "superseded",
        "preliminary",
        "issued",
        "tender",
        "asbuilt",
        "record",
        # copy and working junk
        "copy",
        "of",
        "final",
        "new",
        "use",
        "this",
        "one",
        "latest",
        "current",
        "old",
        "backup",
        "temp",
        "draft",
        "signed",
        "off",
        "check",
        "checked",
    }
)


class NormLevel(StrEnum):
    """How much of a name to strip before comparing."""

    LIGHT = "light"
    MEDIUM = "medium"
    AGGRESSIVE = "aggressive"


@dataclass(slots=True)
class NormalisationResult:
    """A cleaned name plus the story of what was removed.

    The ``stripped`` list is what lets the matcher tell the user *why* two
    names were judged equal, in plain English rather than regex.
    """

    #: The cleaned value. LIGHT and MEDIUM keep separators (single hyphen);
    #: AGGRESSIVE keeps only alphanumerics.
    value: str
    level: NormLevel = NormLevel.LIGHT
    stripped: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.value

    def reason(self) -> str:
        """One human sentence describing the clean, e.g.
        ``the file extension .pdf and the revision suffix 'revc' were removed``.
        """
        if not self.stripped:
            return "the name needed no cleaning"
        if len(self.stripped) == 1:
            return f"{self.stripped[0]} was removed"
        joined = ", ".join(self.stripped[:-1]) + f" and {self.stripped[-1]}"
        return f"{joined} were removed"


def _clean_unicode(name: str) -> str:
    """NFKC first, then map the lookalikes NFKC leaves alone, then lower."""
    text = unicodedata.normalize("NFKC", name or "")
    text = "".join(_LOOKALIKES.get(character, character) for character in text)
    return text.strip().lower()


def _strip_extension(text: str, stripped: list[str]) -> str:
    """Remove a known drawing extension, recording that we did."""
    if "." not in text:
        return text
    candidate = text.rsplit(".", 1)[1].lower()
    if candidate in FILE_EXTENSIONS:
        stripped.append(f"the file extension .{candidate}")
        return text[: -(len(candidate) + 1)]
    return text


def _strip_tail(
    text: str,
    label: str,
    pattern: re.Pattern[str],
    stripped: list[str],
    *,
    self_delimited: bool = False,
) -> str:
    """Strip *pattern* from the very end, only at a token boundary.

    The pattern must reach the end of the string. Unless the pattern is
    self-delimiting (starts with ``_`` or ``(``), whatever precedes the match
    must itself be a boundary or the start of the string — otherwise a suffix
    rule could eat the middle of a longer name, which is exactly the failure
    the "ends inward, never the middle" rule exists to prevent.

    Several overlapping matches can end at the same place (``12-apr-2026``
    and ``apr-2026`` inside ``001-12-apr-2026``). The leftmost one that
    clears the boundary removes the most genuine suffix, so that one wins —
    stripping only ``apr-2026`` would leave a meaningless ``001-12`` behind.
    A lookahead scan is used because plain ``finditer`` never yields
    overlapping matches.
    """
    lookahead = re.compile(f"(?=({pattern.pattern}))", pattern.flags)
    best_start: int | None = None
    for match in lookahead.finditer(text):
        start = match.start(1)
        if not self_delimited and start > 0 and text[start - 1] not in "-_ .(":
            continue
        if best_start is None or start < best_start:
            best_start = start
    if best_start is None:
        return text
    removed = text[best_start:].strip("-_ .()")
    if not removed:
        return text
    stripped.append(f"the {label} {removed!r}")
    return text[:best_start].rstrip("-_ .")


def _strip_underscore_code(text: str, stripped: list[str]) -> str:
    """A code hung off an underscore: ``_D``, ``_P01``, ``_C02``.

    ``_A0``..``_A4`` are sheet sizes, not revisions, so they are left alone
    here and removed at AGGRESSIVE with the other junk.
    """
    match = _UNDERSCORE_CODE.search(text)
    if match is None:
        return text
    code = match.group("code")
    if re.fullmatch(r"a[0-4]", code.lower()) is not None:
        return text
    stripped.append(f"the revision code {code!r}")
    return text[: match.start()].rstrip("-_ ")


def _strip_ends_inward(text: str, stripped: list[str]) -> str:
    """Repeatedly remove revision and date suffixes from the end.

    Order matters: ``RevD`` first (it carries its own marker), then a code
    hung off an underscore or wrapped in parentheses, then dates. The loop
    reruns because ``12-Apr-2026_RevD`` has to lose both.
    """
    rules: list[tuple[str, object]] = [
        ("revision suffix", _REVISION_SUFFIX),
        ("revision code", _strip_underscore_code),
        ("revision", _PAREN_REVISION),
        ("date stamp", _DATE_SUFFIX),
    ]
    changed = True
    while changed:
        changed = False
        for label, rule in rules:
            previous = text
            if isinstance(rule, re.Pattern):
                text = _strip_tail(text, label, rule, stripped, self_delimited=label == "revision")
            else:
                text = rule(text, stripped)  # type: ignore[operator]
            if text != previous:
                changed = True
    return text


def _strip_prefix(text: str, label: str, pattern: re.Pattern[str], stripped: list[str]) -> str:
    """Strip a pattern anchored at the very start of the name."""
    match = pattern.match(text)
    if match is None:
        return text
    stripped.append(f"the {label} {match.group(0).strip('-_ .')!r}")
    return text[match.end() :].lstrip("-_ .")


def _strip_copy_junk(text: str, stripped: list[str]) -> str:
    """'Copy of ...' at the front and '... (1)' / '... - Copy' at the back."""
    prefix = _COPY_PREFIX.match(text)
    if prefix is not None:
        stripped.append("the Windows 'Copy of' prefix")
        text = text[prefix.end() :]
    suffix = _COPY_SUFFIX.search(text)
    if suffix is not None:
        stripped.append(f"the Windows copy marker {suffix.group(0).strip('-_ ')!r}")
        text = text[: suffix.start()].rstrip("-_ ")
    return text


def _is_junk_token(token: str) -> bool:
    """A word to drop at AGGRESSIVE: a junk word or a bare sheet size marker."""
    return token.lower() in JUNK_WORDS or re.fullmatch(r"a[0-4]", token.lower()) is not None


def _strip_junk_words(text: str, stripped: list[str]) -> str:
    """Drop junk words from the two ends (AGGRESSIVE only).

    Token by token, because the words are separated by hyphens at this point.
    A word in the middle of the name is left alone: the user might have
    ``IFC`` as a real part of a discipline code, and removing it from the
    middle would corrupt the number.
    """
    tokens = [token for token in text.split("-") if token]
    while tokens:
        if len(tokens) > 1 and _is_junk_token(tokens[0]):
            stripped.append(f"the junk word {tokens[0]!r}")
            tokens = tokens[1:]
            continue
        if len(tokens) > 1 and _is_junk_token(tokens[-1]):
            stripped.append(f"the junk word {tokens[-1]!r}")
            tokens = tokens[:-1]
            continue
        break
    return "-".join(tokens)


def _unify(text: str) -> str:
    """Every separator becomes a single hyphen; edges and empty runs vanish.

    A trailing ``.`` or space Windows silently strips is a separator like any
    other, so this is where they finally disappear too.
    """
    text = re.sub(r"[-_ ./\\,;:()\[\]&+#\"']+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def normalise(name: str, level: NormLevel = NormLevel.LIGHT) -> NormalisationResult:
    """Clean *name* at *level*, recording what was removed.

    Every rule strips from the ends inward and never from the middle, so a
    drawing number that contains the digit ``01`` survives a clean that
    removes a leading sequence prefix ``01 -``.

    >>> normalise("UVU\\u2013ARC\\u2013001_RevD.pdf", NormLevel.MEDIUM).value
    'uvu-arc-001'
    """
    stripped: list[str] = []
    text = _clean_unicode(name)

    if level is not NormLevel.LIGHT:
        text = _strip_extension(text, stripped)
        text = _strip_prefix(text, "sequence prefix", _SEQUENCE_PREFIX, stripped)
        text = _strip_ends_inward(text, stripped)

    if level is NormLevel.AGGRESSIVE:
        # Copy junk carries its own separators (``(1)``, ``Copy of ...``), so
        # it is peeled off before the rest of the name is unified.
        text = _strip_copy_junk(text, stripped)
        text = _unify(text)
        # Status words can sit *after* the revision suffix (``_RevD_FOR
        # APPROVAL``), so junk words and end suffixes have to be peeled off
        # alternately until nothing more comes away.
        for _ in range(2):
            text = _strip_junk_words(text, stripped)
            text = _strip_ends_inward(text, stripped)
        text = re.sub(r"[^a-z0-9]", "", text)
    else:
        text = _unify(text)

    return NormalisationResult(value=text, level=level, stripped=stripped)


def normalise_filename(name: str, level: NormLevel = NormLevel.MEDIUM) -> NormalisationResult:
    """Clean a full file name (extension included) for filename matching."""
    return normalise(name, level)
