"""Revision schemes, and what "newer" means in each of them.

Real projects number revisions in several different ways, and the scheme
carries meaning of its own:

    Alpha         A, B, C, D      generic; **skips I and O**
    Preliminary   P01, P02        not for construction
    Construction  C01, C02        issued for construction
    Tender        T01, T02        tender issue
    Numeric       00, 01, 02      generic

Two rules that stop the register lying to the user:

* **A scheme change is a status change, not a revision decrease.** `P03 -> C01`
  looks like the revision went backwards. It did not: the drawing moved from
  preliminary to construction. Reporting that as "older" would be wrong, and
  reporting it as unchanged would hide the most commercially significant event
  in a drawing's life.
* **I and O are skipped in alpha schemes**, because they are too easily read as
  1 and 0. So H is followed by J, and N by P. Ordering that assumes a plain
  alphabet gets every comparison after H wrong.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class RevisionScheme(StrEnum):
    ALPHA = "alpha"
    PRELIMINARY = "preliminary"
    CONSTRUCTION = "construction"
    TENDER = "tender"
    NUMERIC = "numeric"
    UNKNOWN = "unknown"


class RevisionComparison(StrEnum):
    NEWER = "newer"
    OLDER = "older"
    SAME = "same"
    STATUS_CHANGE = "status_change"
    INCOMPARABLE = "incomparable"


#: Letters used by alpha revisions, in order. I and O are deliberately absent.
ALPHA_SEQUENCE = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_ALPHA_ORDINAL = {letter: index + 1 for index, letter in enumerate(ALPHA_SEQUENCE)}

#: Prefixed schemes and the status each one represents.
PREFIX_SCHEMES: dict[str, RevisionScheme] = {
    "P": RevisionScheme.PRELIMINARY,
    "C": RevisionScheme.CONSTRUCTION,
    "T": RevisionScheme.TENDER,
}

#: How far through the design life each status sits. Used to describe a
#: status change in words, never to decide "newer".
STATUS_RANK: dict[RevisionScheme, int] = {
    RevisionScheme.PRELIMINARY: 1,
    RevisionScheme.TENDER: 2,
    RevisionScheme.CONSTRUCTION: 3,
}

#: Noise around a revision code: "Rev A", "REV.A", "rev_a", "-A", "(A)".
_NOISE = re.compile(r"^[\s\-_(\[]*(?:REV(?:ISION)?|ISSUE)?[\s.:\-_]*|[\s)\]]*$", re.IGNORECASE)

_PREFIXED = re.compile(r"^([PCT])\s*[-_.]?\s*(\d{1,3})$", re.IGNORECASE)
_ALPHA = re.compile(r"^([A-Z]{1,2})$", re.IGNORECASE)
_ALPHA_WITH_NUMBER = re.compile(r"^([A-Z])\s*[-_.]?\s*(\d{1,2})$", re.IGNORECASE)
_NUMERIC = re.compile(r"^(\d{1,3})$")


@dataclass(frozen=True, slots=True)
class ParsedRevision:
    """A revision code, understood."""

    raw: str
    scheme: RevisionScheme
    #: Position within the scheme. Higher is later. None when unparseable.
    ordinal: int | None
    #: Cleaned display form, e.g. "P03", "C", "01".
    normalised: str

    @property
    def is_known(self) -> bool:
        return self.scheme is not RevisionScheme.UNKNOWN and self.ordinal is not None

    @property
    def is_status_scheme(self) -> bool:
        """True for the schemes that also say what the drawing is issued for."""
        return self.scheme in STATUS_RANK

    def __str__(self) -> str:
        return self.normalised or self.raw


def clean_revision(raw: str | None) -> str:
    """Strip the wrapping off a revision code.

    `Rev A`, `REV.A`, `rev_a`, `-A` and `(A)` all reduce to `A`.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    # Remove a leading "Rev"/"Issue" and any surrounding punctuation.
    text = _NOISE.sub("", text)
    return text.strip().strip(".:-_()[] ").upper()


def parse_revision(raw: str | None) -> ParsedRevision:
    """Work out which scheme a revision code belongs to, and where it sits."""
    text = clean_revision(raw)
    if not text:
        return ParsedRevision(raw or "", RevisionScheme.UNKNOWN, None, "")

    match = _PREFIXED.match(text)
    if match:
        prefix = match.group(1).upper()
        number = int(match.group(2))
        return ParsedRevision(
            raw or "",
            PREFIX_SCHEMES[prefix],
            number,
            f"{prefix}{number:02d}",
        )

    match = _NUMERIC.match(text)
    if match:
        number = int(match.group(1))
        return ParsedRevision(raw or "", RevisionScheme.NUMERIC, number, f"{number:02d}")

    match = _ALPHA.match(text)
    if match:
        letters = match.group(1).upper()
        ordinal = _alpha_ordinal(letters)
        if ordinal is not None:
            return ParsedRevision(raw or "", RevisionScheme.ALPHA, ordinal, letters)

    # "A1", "B2": an alpha revision with a sub-number. The letter dominates.
    match = _ALPHA_WITH_NUMBER.match(text)
    if match:
        letters = match.group(1).upper()
        ordinal = _alpha_ordinal(letters)
        if ordinal is not None:
            sub = int(match.group(2))
            # Scale so B1 still sorts after A9.
            return ParsedRevision(
                raw or "", RevisionScheme.ALPHA, ordinal * 100 + sub, f"{letters}{sub}"
            )

    return ParsedRevision(raw or "", RevisionScheme.UNKNOWN, None, text)


def _alpha_ordinal(letters: str) -> int | None:
    """Position in the alpha sequence, skipping I and O. AA follows Z."""
    total = 0
    for letter in letters:
        value = _ALPHA_ORDINAL.get(letter)
        if value is None:
            return None  # an I or an O, which the scheme does not use
        total = total * len(ALPHA_SEQUENCE) + value
    return total


def detect_scheme(revisions: Iterable[str | None]) -> RevisionScheme:
    """The scheme a set of revision codes belongs to.

    Mixed sets are common while a job moves from preliminary to construction,
    so the most frequent known scheme wins rather than the first one seen.
    """
    counts: dict[RevisionScheme, int] = {}
    for raw in revisions:
        parsed = parse_revision(raw)
        if parsed.is_known:
            counts[parsed.scheme] = counts.get(parsed.scheme, 0) + 1

    if not counts:
        return RevisionScheme.UNKNOWN
    return max(counts.items(), key=lambda pair: pair[1])[0]


def compare_revisions(old: str | None, new: str | None) -> RevisionComparison:
    """How *new* relates to *old*."""
    first = parse_revision(old)
    second = parse_revision(new)

    if not first.is_known or not second.is_known:
        return RevisionComparison.INCOMPARABLE

    if first.scheme is not second.scheme:
        # The drawing changed status. Not a decrease, whatever the numbers do.
        return RevisionComparison.STATUS_CHANGE

    assert first.ordinal is not None and second.ordinal is not None
    if second.ordinal > first.ordinal:
        return RevisionComparison.NEWER
    if second.ordinal < first.ordinal:
        return RevisionComparison.OLDER
    return RevisionComparison.SAME


def is_significant(old: str | None, new: str | None) -> bool:
    """Whether this revision change is worth the user's attention.

    A status change counts even when the drawing itself is identical: moving
    from preliminary to construction changes what the drawing means, and
    therefore what it is worth.
    """
    return compare_revisions(old, new) in {
        RevisionComparison.NEWER,
        RevisionComparison.STATUS_CHANGE,
    }


def describe_change(old: str | None, new: str | None) -> str:
    """One plain sentence about the revision change, for the register."""
    comparison = compare_revisions(old, new)
    first = parse_revision(old)
    second = parse_revision(new)

    if comparison is RevisionComparison.SAME:
        return f"Still at revision {second}."
    if comparison is RevisionComparison.NEWER:
        return f"Revised from {first} to {second}."
    if comparison is RevisionComparison.OLDER:
        return (
            f"The current issue shows {second}, which is earlier than {first} "
            "in the previous issue. Check the issue is the right one."
        )
    if comparison is RevisionComparison.STATUS_CHANGE:
        return _describe_status_change(first, second)

    if not first.is_known and not second.is_known:
        return "No revision could be read from either issue."
    unknown = old if not first.is_known else new
    return f"The revision {unknown!r} could not be understood."


def _describe_status_change(first: ParsedRevision, second: ParsedRevision) -> str:
    labels = {
        RevisionScheme.PRELIMINARY: "preliminary",
        RevisionScheme.CONSTRUCTION: "construction",
        RevisionScheme.TENDER: "tender",
        RevisionScheme.ALPHA: "an unnumbered",
        RevisionScheme.NUMERIC: "a numbered",
    }
    from_label = labels.get(first.scheme, str(first.scheme))
    to_label = labels.get(second.scheme, str(second.scheme))

    sentence = (
        f"Status changed from {from_label} ({first}) to {to_label} ({second}). "
        "This is a status change, not a revision decrease."
    )
    if (
        first.scheme in STATUS_RANK
        and second.scheme in STATUS_RANK
        and STATUS_RANK[second.scheme] < STATUS_RANK[first.scheme]
    ):
        sentence += " The drawing has moved backwards in the issue sequence; check why."
    return sentence
