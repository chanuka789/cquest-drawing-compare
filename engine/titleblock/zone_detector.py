"""Find the strip of the sheet that holds the title block.

Almost every drawing office puts the title block in the right-hand strip or
along the bottom. Rather than trying to detect the box graphically, this uses
the two candidate zones from the plan — rightmost 25%, bottom 20% — and scores
them by how much title-block-like text each one holds.

Working in *fractions* of the page box rather than points is what makes this
survive real files: an A0 sheet, an A3 sheet and a sheet whose media box is
centred on the origin all give the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from engine.extract.text_extractor import PageText, TextItem


class ZoneName(StrEnum):
    RIGHT = "right"
    BOTTOM = "bottom"
    BOTTOM_RIGHT = "bottom_right"
    WHOLE_SHEET = "whole_sheet"


#: Fractional bounds of each candidate zone: (x_from, x_to, y_from, y_to).
ZONE_BOUNDS: dict[ZoneName, tuple[float, float, float, float]] = {
    # The corner first: on most sheets this is where the number actually is.
    ZoneName.BOTTOM_RIGHT: (0.70, 1.0, 0.0, 0.35),
    ZoneName.RIGHT: (0.75, 1.0, 0.0, 1.0),
    ZoneName.BOTTOM: (0.0, 1.0, 0.0, 0.20),
    ZoneName.WHOLE_SHEET: (0.0, 1.0, 0.0, 1.0),
}

#: Words that only ever appear in a title block. Used to score a zone.
TITLE_BLOCK_WORDS: frozenset[str] = frozenset(
    {
        "drawing",
        "dwg",
        "drg",
        "sheet",
        "document",
        "project",
        "rev",
        "revision",
        "scale",
        "date",
        "drawn",
        "checked",
        "approved",
        "title",
        "client",
        "consultant",
        "architect",
        "status",
    }
)


@dataclass(slots=True)
class Zone:
    """One candidate title block region and the text inside it."""

    name: ZoneName
    x_from: float
    x_to: float
    y_from: float
    y_to: float
    items: list[TextItem]
    score: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.items

    def text(self) -> str:
        ordered = sorted(self.items, key=lambda item: (-item.fy, item.fx))
        return "\n".join(item.clean for item in ordered if item.clean)


def score_zone(items: list[TextItem]) -> int:
    """How much this text looks like a title block."""
    if not items:
        return 0

    found: set[str] = set()
    for item in items:
        lowered = item.clean.lower()
        for word in TITLE_BLOCK_WORDS:
            if word in lowered:
                found.add(word)
    return len(found)


def detect_zones(page: PageText) -> list[Zone]:
    """Return the candidate zones, best first.

    The whole sheet is always included as a last resort, so a sheet with an
    unusual layout still gets searched rather than silently returning nothing.
    """
    zones: list[Zone] = []
    for name, (x_from, x_to, y_from, y_to) in ZONE_BOUNDS.items():
        items = page.in_zone(x_from, x_to, y_from, y_to)
        zones.append(
            Zone(
                name=name,
                x_from=x_from,
                x_to=x_to,
                y_from=y_from,
                y_to=y_to,
                items=items,
                score=score_zone(items),
            )
        )

    # Best score first; the whole sheet stays last so it is only used when
    # the focused zones found nothing.
    zones.sort(key=lambda zone: (zone.name is ZoneName.WHOLE_SHEET, -zone.score, zone.name))
    return zones


def best_zone(page: PageText) -> Zone | None:
    """The most title-block-like zone, or None when the page has no text."""
    zones = [zone for zone in detect_zones(page) if not zone.is_empty]
    return zones[0] if zones else None
