"""Task 5.1 — detect the sheet template once, apply it to hundreds of sheets.

Every project uses two or three sheet templates. The title block sits in the
same place on all three hundred sheets, so detecting it three hundred times is
three hundred chances to get it wrong — and automatic detection will never be
perfect on every one of them.

The alternative is the whole point of this module:

1. fingerprint each sheet's frame geometry,
2. cluster the sheets by that fingerprint — typically two or three clusters,
3. detect the zones **once per cluster**, on a representative sheet,
4. show the user, who adjusts it in ten seconds,
5. apply the confirmed result to every sheet in the cluster.

That turns an unreliable three-hundred-times-repeated detection into one
reliable detection a human confirmed. A confirmed template is worth more than
any amount of cleverness.

The fingerprint is deliberately coarse. It must not split a cluster because
one sheet's border line landed half a millimetre further left, and it must
not merge a landscape A1 with a portrait A3. Quantising the line positions to
a grid of the page size does both.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from engine.masking.titleblock_zones import LineSet, detect_zones, find_long_lines
from engine.masking.types import FracRect, ProtectedRegion, SheetView, Zone

#: Line positions are quantised to this fraction of the page before hashing.
#: 1/64 of an A1 is about 37 mm — coarse enough that plotting jitter never
#: splits a cluster, fine enough that two genuinely different frames never
#: merge.
FINGERPRINT_GRID = 1.0 / 64.0
#: Page sizes within this fraction of each other are the same size.
SIZE_TOLERANCE = 0.02
#: Fingerprints agreeing on this share of their lines are the same template.
CLUSTER_SIMILARITY = 0.75


@dataclass(slots=True)
class FrameFingerprint:
    """The structure of one sheet's frame, independent of its content."""

    width_px: int = 0
    height_px: int = 0
    aspect: float = 1.0
    orientation: str = "landscape"
    #: Quantised positions of the long lines, as page fractions.
    horizontals: tuple[float, ...] = ()
    verticals: tuple[float, ...] = ()
    #: The rectangles those lines enclose, as a count per size band.
    region_count: int = 0
    digest: str = ""

    def similarity(self, other: FrameFingerprint) -> float:
        """How alike two frames are, from 0 to 1.

        Orientation and aspect ratio are gates, not scores: a portrait sheet
        is never the same template as a landscape one, however many of its
        lines happen to land in the same places.
        """
        if self.orientation != other.orientation:
            return 0.0
        if abs(self.aspect - other.aspect) > SIZE_TOLERANCE * max(self.aspect, other.aspect):
            return 0.0

        horizontal = _overlap(self.horizontals, other.horizontals)
        vertical = _overlap(self.verticals, other.verticals)
        weights = len(self.horizontals) + len(self.verticals)
        other_weights = len(other.horizontals) + len(other.verticals)
        if weights == 0 and other_weights == 0:
            return 1.0
        matched = horizontal + vertical
        return 2.0 * matched / max(weights + other_weights, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "aspect": round(self.aspect, 4),
            "orientation": self.orientation,
            "horizontals": list(self.horizontals),
            "verticals": list(self.verticals),
            "region_count": self.region_count,
            "digest": self.digest,
        }


def _overlap(first: tuple[float, ...], second: tuple[float, ...]) -> int:
    """How many quantised positions the two sets share."""
    remaining = list(second)
    matched = 0
    for value in first:
        for index, candidate in enumerate(remaining):
            if abs(value - candidate) <= FINGERPRINT_GRID:
                matched += 1
                remaining.pop(index)
                break
    return matched


def _quantise(values: list[float]) -> tuple[float, ...]:
    snapped = sorted({round(value / FINGERPRINT_GRID) * FINGERPRINT_GRID for value in values})
    return tuple(round(value, 4) for value in snapped)


def _enclosed_regions(lines: LineSet) -> int:
    """How many rectangles the long lines enclose.

    Only the count is kept. The positions are already in the fingerprint, and
    a count is a cheap, stable summary of how busy a frame is.
    """
    if len(lines.horizontals) < 2 or len(lines.verticals) < 2:
        return 0
    return (len(lines.horizontals) - 1) * (len(lines.verticals) - 1)


def fingerprint_frame(sheet: SheetView, lines: LineSet | None = None) -> FrameFingerprint:
    """Summarise one sheet's frame geometry into a comparable fingerprint."""
    lines = lines if lines is not None else find_long_lines(sheet.gray)
    width = max(sheet.width_px, 1)
    height = max(sheet.height_px, 1)
    aspect = width / height

    fingerprint = FrameFingerprint(
        width_px=width,
        height_px=height,
        aspect=aspect,
        orientation="landscape" if width >= height else "portrait",
        horizontals=_quantise(lines.horizontals),
        verticals=_quantise(lines.verticals),
        region_count=_enclosed_regions(lines),
    )

    digest = hashlib.blake2b(digest_size=8)
    digest.update(fingerprint.orientation.encode())
    digest.update(f"{round(aspect, 2)}|".encode())
    digest.update(",".join(f"{value:.4f}" for value in fingerprint.horizontals).encode())
    digest.update(b"|")
    digest.update(",".join(f"{value:.4f}" for value in fingerprint.verticals).encode())
    fingerprint.digest = digest.hexdigest()
    return fingerprint


@dataclass(slots=True)
class SheetTemplate:
    """One cluster of sheets that share a frame, and the zones for all of them."""

    template_id: str = ""
    fingerprint: FrameFingerprint = field(default_factory=FrameFingerprint)
    #: Indices into the list passed to :func:`cluster_sheets`.
    members: list[int] = field(default_factory=list)
    #: The member whose fingerprint sits closest to the cluster centre.
    representative: int = -1
    zones: list[Zone] = field(default_factory=list)
    protected: list[ProtectedRegion] = field(default_factory=list)
    #: True once a user has looked at it. Detection is never trusted alone.
    confirmed: bool = False

    @property
    def sheet_count(self) -> int:
        return len(self.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "fingerprint": self.fingerprint.as_dict(),
            "members": list(self.members),
            "representative": self.representative,
            "sheet_count": self.sheet_count,
            "zones": [zone.as_dict() for zone in self.zones],
            "protected": [region.as_dict() for region in self.protected],
            "confirmed": self.confirmed,
        }


def cluster_sheets(
    sheets: list[SheetView], similarity: float = CLUSTER_SIMILARITY
) -> list[SheetTemplate]:
    """Group sheets by frame fingerprint and pick one representative each.

    A project normally falls into two or three clusters: the general
    arrangement sheets, the detail sheets, and whatever a consultant issued
    on their own title block.
    """
    fingerprints = [fingerprint_frame(sheet) for sheet in sheets]
    templates: list[SheetTemplate] = []

    for index, fingerprint in enumerate(fingerprints):
        for template in templates:
            if template.fingerprint.similarity(fingerprint) >= similarity:
                template.members.append(index)
                break
        else:
            templates.append(
                SheetTemplate(
                    template_id=f"template-{fingerprint.digest[:8]}",
                    fingerprint=fingerprint,
                    members=[index],
                )
            )

    for template in templates:
        template.representative = _most_typical(template, fingerprints)

    templates.sort(key=lambda template: template.sheet_count, reverse=True)
    logger.info(
        "Sheet templates | sheets={} | clusters={} | sizes={}",
        len(sheets),
        len(templates),
        [template.sheet_count for template in templates],
    )
    return templates


def _most_typical(template: SheetTemplate, fingerprints: list[FrameFingerprint]) -> int:
    """The member most like every other member: the cluster's centre.

    Picking the first member instead would hand the user whichever sheet
    happened to be scanned first, which is as likely as not to be the one odd
    sheet in the set.
    """
    if len(template.members) == 1:
        return template.members[0]
    best_index = template.members[0]
    best_score = -1.0
    for index in template.members:
        score = sum(
            fingerprints[index].similarity(fingerprints[other])
            for other in template.members
            if other != index
        )
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def detect_template_zones(template: SheetTemplate, sheet: SheetView) -> SheetTemplate:
    """Run zone detection once, on the cluster's representative sheet."""
    detected = detect_zones(sheet)
    template.zones = detected.zones
    template.protected = detected.protected
    return template


def apply_template(
    template: SheetTemplate, sheet: SheetView
) -> tuple[list[Zone], list[ProtectedRegion]]:
    """Map a template's zones onto another sheet in the same cluster.

    Zones are stored as page fractions, so a sheet of the same proportions
    needs no adjustment at all. A sheet whose aspect ratio differs slightly —
    an A1 trimmed a few millimetres, which happens — has its zones stretched
    to keep them against the same edges rather than floating into the drawing.
    """
    source = template.fingerprint
    if source.width_px <= 0 or sheet.width_px <= 0:
        return list(template.zones), list(template.protected)

    target_aspect = sheet.width_px / max(sheet.height_px, 1)
    ratio = target_aspect / source.aspect if source.aspect else 1.0
    if abs(ratio - 1.0) < 0.005:
        return list(template.zones), list(template.protected)

    zones = [_stretch_zone(zone, ratio) for zone in template.zones]
    protected = [
        ProtectedRegion(
            type=region.type,
            rect=_stretch_rect(region.rect, ratio),
            label=region.label,
            confidence=region.confidence,
            evidence=list(region.evidence),
        )
        for region in template.protected
    ]
    return zones, protected


def _stretch_rect(rect: FracRect, ratio: float) -> FracRect:
    """Keep a zone against the edge it was detected against.

    A zone in the right-hand strip of the sheet must stay in the right-hand
    strip when the sheet is a little wider, not drift towards the middle, so
    the distance from the nearer edge is what is preserved.
    """

    def adjust(value: float) -> float:
        if value > 0.5:
            return 1.0 - (1.0 - value) / ratio
        return value / ratio

    return FracRect(adjust(rect.x0), rect.y0, adjust(rect.x1), rect.y1).clipped()


def _stretch_zone(zone: Zone, ratio: float) -> Zone:
    return Zone(
        type=zone.type,
        rect=_stretch_rect(zone.rect, ratio),
        polygon=list(zone.polygon),
        label=zone.label,
        confidence=zone.confidence,
        evidence=list(zone.evidence),
        enabled=zone.enabled,
        user_edited=zone.user_edited,
        ink_only=zone.ink_only,
        ink_threshold=zone.ink_threshold,
        match_text=zone.match_text,
    )
