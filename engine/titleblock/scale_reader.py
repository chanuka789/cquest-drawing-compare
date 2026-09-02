"""Read the drawing scale from title block text.

The scale matters later: tolerances are held in millimetres at true scale, so
a 25 mm tolerance is a different number of pixels on a 1:50 sheet than on a
1:200 one. Getting it wrong quietly is worse than not reading it at all, so
anything ambiguous comes back as unknown rather than as a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 1:100, 1 : 100, 1/100, 1-100
RATIO_PATTERN = re.compile(r"\b1\s*[:/\-]\s*(\d{1,5})\b")

#: Metric bar scales written out, e.g. "1:100 @ A1"
AT_SIZE_PATTERN = re.compile(r"@\s*(A[0-4])\b", re.IGNORECASE)

#: Scales that are words rather than numbers.
WORD_SCALES: dict[str, str] = {
    "nts": "NTS",
    "n.t.s": "NTS",
    "n.t.s.": "NTS",
    "not to scale": "NTS",
    "as shown": "As shown",
    "as noted": "As shown",
    "various": "As shown",
    "refer to drawing": "As shown",
}


@dataclass(frozen=True, slots=True)
class ScaleReading:
    """A scale, and how sure we are of it."""

    text: str | None
    #: The denominator of 1:N, when there is one. None for NTS or unknown.
    ratio: int | None = None
    sheet_size: str | None = None
    confidence: float = 0.0

    @property
    def is_known(self) -> bool:
        return self.text is not None

    @property
    def is_numeric(self) -> bool:
        return self.ratio is not None

    def __str__(self) -> str:
        return self.text or "Unknown"


def read_scale(text: str) -> ScaleReading:
    """Find the scale in a piece of title block text.

    Several scales on one sheet ("1:100" and "1:50" for a detail) is common,
    so the *first* is taken as the sheet scale and the confidence is lowered
    to say the sheet has more than one.
    """
    if not text:
        return ScaleReading(text=None)

    lowered = " ".join(text.lower().split())

    for phrase, label in WORD_SCALES.items():
        if phrase in lowered:
            return ScaleReading(text=label, ratio=None, confidence=0.9)

    matches = RATIO_PATTERN.findall(text)
    if matches:
        try:
            ratio = int(matches[0])
        except ValueError:
            return ScaleReading(text=None)

        if ratio <= 0:
            return ScaleReading(text=None)

        size_match = AT_SIZE_PATTERN.search(text)
        # One clear scale is trustworthy; several means the sheet has details
        # at other scales and the reader should check.
        confidence = 0.95 if len(matches) == 1 else 0.6
        return ScaleReading(
            text=f"1:{ratio}",
            ratio=ratio,
            sheet_size=size_match.group(1).upper() if size_match else None,
            confidence=confidence,
        )

    return ScaleReading(text=None)


def scales_are_equivalent(first: str | None, second: str | None) -> bool:
    """True when two scale strings mean the same thing.

    `1:100`, `1 : 100` and `1/100` are the same scale written three ways.
    """
    if first is None or second is None:
        return first == second

    left = read_scale(first)
    right = read_scale(second)

    if left.ratio is not None and right.ratio is not None:
        return left.ratio == right.ratio
    return (left.text or "").upper() == (right.text or "").upper()
