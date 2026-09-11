"""Task 5.4 (backend) — the mask engine.

A :class:`MaskSet` is the single answer to "is this thing compared?", and it
answers that question identically for a raster image, a list of text items and
a list of vector paths. Three streams that mask differently would report the
same region three different ways, and merging those results would be
guesswork.

Two rules the engine enforces rather than trusts callers to remember:

* **Protected beats masked.** A region the detector deliberately kept — north
  arrow, scale bar, key plan, general notes — stays compared even when a
  title block zone overlaps it. Masking wins everywhere else.
* **Disabled is not deleted.** A zone the user switched off keeps its
  detection and its evidence, so turning it back on costs one click and the
  mask editor can still explain what it was.

Masks are saved per *sheet template*, not per sheet. Detecting a title block
three hundred times is unreliable; detecting it on three representative
sheets, showing the user, and applying the confirmed result to every sheet in
each cluster is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
from loguru import logger

from engine.extract.text_extractor import TextItem
from engine.masking.types import (
    FracRect,
    ProtectedRegion,
    ProtectedType,
    SheetView,
    Zone,
    ZoneType,
)
from engine.storage.paths import get_app_paths
from engine.utils.errors import NotFoundError, ValidationError

#: Schema marker written into every saved mask file.
MASK_FILE_VERSION = 1


@dataclass(slots=True)
class MaskSet:
    """A named collection of exclusion zones plus the protected regions."""

    name: str = "default"
    #: The sheet template this mask belongs to, when it came from one.
    template_id: str = ""
    zones: list[Zone] = field(default_factory=list)
    protected: list[ProtectedRegion] = field(default_factory=list)
    #: Sheets this mask was applied to, for the run record.
    applied_sheet_count: int = 0

    # ── Queries ──────────────────────────────────────────────────────

    @property
    def enabled_zones(self) -> list[Zone]:
        return [zone for zone in self.zones if zone.enabled]

    def is_protected(self, fx: float, fy: float) -> bool:
        return any(region.rect.contains(fx, fy) for region in self.protected)

    @property
    def ink_only_zones(self) -> list[Zone]:
        """Zones that suppress their own ink rather than everything beneath."""
        return [zone for zone in self.zones if zone.enabled and zone.ink_only]

    def covers(self, fx: float, fy: float) -> bool:
        """True when a point in page fractions is excluded from comparison."""
        if self.is_protected(fx, fy):
            return False
        return any(zone.contains(fx, fy) for zone in self.zones)

    def clean_text(self, fx: float, fy: float, text: str) -> str:
        """Text with any overlapping watermark's stray glyphs removed."""
        for zone in self.ink_only_zones:
            text = zone.clean_text(fx, fy, text)
        return text

    def covers_text(self, fx: float, fy: float, normalised_text: str) -> bool:
        """True when one piece of text is excluded from comparison.

        Differs from :meth:`covers` only for ink-only zones, where the
        string has to be the watermark's own before it is dropped.
        """
        if self.is_protected(fx, fy):
            return False
        return any(zone.suppresses_text(fx, fy, normalised_text) for zone in self.zones)

    def covering_zone(self, fx: float, fy: float) -> Zone | None:
        """Which zone excluded a point — for the debug view's explanation."""
        if self.is_protected(fx, fy):
            return None
        for zone in self.zones:
            if zone.contains(fx, fy):
                return zone
        return None

    # ── Editing ──────────────────────────────────────────────────────

    def add_zone(self, zone: Zone) -> Zone:
        self.zones.append(zone)
        return zone

    def add_user_rect(self, rect: FracRect, label: str = "User zone") -> Zone:
        return self.add_zone(
            Zone(
                type=ZoneType.USER,
                rect=rect.clipped(),
                label=label,
                confidence=1.0,
                evidence=["Drawn by the user in the mask editor."],
                user_edited=True,
            )
        )

    def add_user_polygon(self, points: list[tuple[float, float]], label: str = "User zone") -> Zone:
        if len(points) < 3:
            raise ValidationError("A polygon zone needs at least three points.")
        return self.add_zone(
            Zone(
                type=ZoneType.USER,
                rect=FracRect.from_points(points).clipped(),
                polygon=[(float(x), float(y)) for x, y in points],
                label=label,
                confidence=1.0,
                evidence=["Drawn by the user in the mask editor."],
                user_edited=True,
            )
        )

    def protect(self, rect: FracRect, label: str = "Protected region") -> ProtectedRegion:
        region = ProtectedRegion(
            type=ProtectedType.USER,
            rect=rect.clipped(),
            label=label,
            confidence=1.0,
            evidence=["Marked by the user as always compared."],
        )
        self.protected.append(region)
        return region

    def set_enabled(self, index: int, enabled: bool) -> Zone:
        if not 0 <= index < len(self.zones):
            raise NotFoundError("That zone is no longer in the mask.")
        zone = self.zones[index]
        zone.enabled = enabled
        zone.user_edited = True
        return zone

    def remove(self, index: int) -> Zone:
        if not 0 <= index < len(self.zones):
            raise NotFoundError("That zone is no longer in the mask.")
        return self.zones.pop(index)

    # ── Serialisation ────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": MASK_FILE_VERSION,
            "name": self.name,
            "template_id": self.template_id,
            "zones": [zone.as_dict() for zone in self.zones],
            "protected": [region.as_dict() for region in self.protected],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MaskSet:
        mask = MaskSet(
            name=str(data.get("name", "default")),
            template_id=str(data.get("template_id", "")),
        )
        for entry in data.get("zones", []):
            rect = entry.get("rect", {})
            mask.zones.append(
                Zone(
                    type=ZoneType(entry.get("type", "user")),
                    rect=FracRect(
                        float(rect.get("x0", 0.0)),
                        float(rect.get("y0", 0.0)),
                        float(rect.get("x1", 0.0)),
                        float(rect.get("y1", 0.0)),
                    ),
                    polygon=[(float(x), float(y)) for x, y in entry.get("polygon", [])],
                    label=str(entry.get("label", "")),
                    confidence=float(entry.get("confidence", 1.0)),
                    evidence=[str(item) for item in entry.get("evidence", [])],
                    enabled=bool(entry.get("enabled", True)),
                    user_edited=bool(entry.get("user_edited", False)),
                    ink_only=bool(entry.get("ink_only", False)),
                    ink_threshold=int(entry.get("ink_threshold", 130)),
                    match_text=str(entry.get("match_text", "")),
                )
            )
        for entry in data.get("protected", []):
            rect = entry.get("rect", {})
            mask.protected.append(
                ProtectedRegion(
                    type=ProtectedType(entry.get("type", "user_protected")),
                    rect=FracRect(
                        float(rect.get("x0", 0.0)),
                        float(rect.get("y0", 0.0)),
                        float(rect.get("x1", 0.0)),
                        float(rect.get("y1", 0.0)),
                    ),
                    label=str(entry.get("label", "")),
                    confidence=float(entry.get("confidence", 1.0)),
                    evidence=[str(item) for item in entry.get("evidence", [])],
                )
            )
        return mask


# ── Building ────────────────────────────────────────────────────────────


def build_mask(
    sheet: SheetView,
    *,
    template: Any | None = None,
    detections: list[Any] | None = None,
    user_zones: list[Zone] | None = None,
    name: str = "default",
) -> MaskSet:
    """Assemble one sheet's mask from every source, in priority order.

    Template zones come first because they were confirmed by a human; per
    sheet detections (watermarks, stamps, annotations) come next; the user's
    own zones come last and are never overridden.
    """
    mask = MaskSet(name=name)

    if template is not None:
        template_zones = getattr(template, "zones", None)
        if template_zones:
            mask.zones.extend(_copy_zones(template_zones))
        template_protected = getattr(template, "protected", None)
        if template_protected:
            mask.protected.extend(list(template_protected))
        mask.template_id = str(getattr(template, "template_id", "") or "")

    for detection in detections or []:
        zone = detection if isinstance(detection, Zone) else None
        if zone is None and hasattr(detection, "as_zone"):
            from engine.masking.watermark_detect import detections_to_zones

            mask.zones.extend(detections_to_zones([detection]))
            continue
        if zone is not None:
            mask.zones.append(zone)

    for zone in user_zones or []:
        zone.user_edited = True
        mask.zones.append(zone)

    logger.debug(
        "Built mask | zones={} | protected={} | template={}",
        len(mask.zones),
        len(mask.protected),
        mask.template_id or "none",
    )
    return mask


def _copy_zones(zones: list[Zone]) -> list[Zone]:
    return [
        Zone(
            type=zone.type,
            rect=zone.rect,
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
        for zone in zones
    ]


# ── Applying ────────────────────────────────────────────────────────────


def mask_array(mask: MaskSet, width_px: int, height_px: int) -> np.ndarray:
    """A boolean array, True where a pixel is excluded from comparison."""
    covered = np.zeros((max(height_px, 1), max(width_px, 1)), dtype=bool)
    if width_px <= 0 or height_px <= 0:
        return covered

    ys = (np.arange(height_px) + 0.5) / height_px
    xs = (np.arange(width_px) + 0.5) / width_px

    for zone in mask.enabled_zones:
        if zone.ink_only:
            # Its area is not excluded, only its own pale ink — which
            # _pale_ink_mask handles, because it needs the image to do it.
            continue
        if zone.polygon:
            _fill_polygon(covered, zone.polygon, width_px, height_px)
            continue
        rect = zone.rect.clipped()
        x_slice = np.searchsorted(xs, [rect.x0, rect.x1])
        y_slice = np.searchsorted(ys, [rect.y0, rect.y1])
        covered[y_slice[0] : y_slice[1], x_slice[0] : x_slice[1]] = True

    for region in mask.protected:
        rect = region.rect.clipped()
        x_slice = np.searchsorted(xs, [rect.x0, rect.x1])
        y_slice = np.searchsorted(ys, [rect.y0, rect.y1])
        covered[y_slice[0] : y_slice[1], x_slice[0] : x_slice[1]] = False

    return covered


def _fill_polygon(
    covered: np.ndarray, polygon: list[tuple[float, float]], width_px: int, height_px: int
) -> None:
    import cv2

    points = np.array(
        [[round(x * width_px), round(y * height_px)] for x, y in polygon], dtype=np.int32
    )
    layer = np.zeros(covered.shape, dtype=np.uint8)
    cv2.fillPoly(layer, [points], 1)
    covered |= layer.astype(bool)


def apply_mask_to_image(
    image: np.ndarray, mask: MaskSet, *, fill: int = 255, invert: bool = False
) -> np.ndarray:
    """A copy of *image* with masked regions filled.

    Ink-only zones are handled by value rather than by area: inside such a
    zone only pixels paler than its threshold are erased, so a grey watermark
    goes and the black drawing under it stays. ``invert=True`` gives the
    debug view — everything that is *not* compared.
    """
    if image.size == 0:
        return image.copy()
    height, width = image.shape[:2]
    covered = mask_array(mask, width, height)
    pale = _pale_ink_mask(image, mask, width, height)
    if invert:
        covered = ~(covered | pale)
        output = image.copy()
        output[covered] = fill
        return output

    output = image.copy()
    output[covered | pale] = fill
    return output


def _pale_ink_mask(image: np.ndarray, mask: MaskSet, width: int, height: int) -> np.ndarray:
    """Pixels that belong to an ink-only zone's own pale ink."""
    pale = np.zeros((height, width), dtype=bool)
    zones = mask.ink_only_zones
    if not zones or image.ndim != 2:
        return pale

    ys = (np.arange(height) + 0.5) / height
    xs = (np.arange(width) + 0.5) / width
    for zone in zones:
        rect = zone.rect.clipped()
        x_slice = np.searchsorted(xs, [rect.x0, rect.x1])
        y_slice = np.searchsorted(ys, [rect.y0, rect.y1])
        patch = image[y_slice[0] : y_slice[1], x_slice[0] : x_slice[1]]
        if patch.size == 0:
            continue
        # Ink, but paler than the drawing's own lines.
        pale[y_slice[0] : y_slice[1], x_slice[0] : x_slice[1]] |= (patch > zone.ink_threshold) & (
            patch < 250
        )

    for region in mask.protected:
        rect = region.rect.clipped()
        x_slice = np.searchsorted(xs, [rect.x0, rect.x1])
        y_slice = np.searchsorted(ys, [rect.y0, rect.y1])
        pale[y_slice[0] : y_slice[1], x_slice[0] : x_slice[1]] = False
    return pale


ItemT = TypeVar("ItemT")


def apply_mask_to_items(
    items: list[TextItem], mask: MaskSet, sheet: SheetView
) -> tuple[list[TextItem], list[TextItem]]:
    """Split text items into (compared, excluded)."""
    import unicodedata

    kept: list[TextItem] = []
    dropped: list[TextItem] = []
    for item in items:
        fx, fy = sheet.item_centre(item)
        normalised = " ".join(unicodedata.normalize("NFKC", item.clean).upper().split())
        (dropped if mask.covers_text(fx, fy, normalised) else kept).append(item)
    return kept, dropped


def apply_mask_to_points(
    points: list[tuple[float, float]], mask: MaskSet, width_px: int, height_px: int
) -> list[bool]:
    """For each pixel-space point, True when it is excluded."""
    result: list[bool] = []
    for x, y in points:
        fx = x / width_px if width_px else 0.0
        fy = y / height_px if height_px else 0.0
        result.append(mask.covers(fx, fy))
    return result


def apply_mask(subject: Any, mask: MaskSet, sheet: SheetView | None = None, **kwargs: Any) -> Any:
    """One entry point for all three shapes of subject.

    A numpy array is masked as an image; a list of text items is filtered; any
    other list of objects is filtered by a ``bbox_px`` or ``centre_px``
    attribute, which is what the vector path and hatch region types carry.
    """
    if isinstance(subject, np.ndarray):
        return apply_mask_to_image(subject, mask, **kwargs)

    if isinstance(subject, list) and subject and isinstance(subject[0], TextItem):
        if sheet is None:
            raise ValidationError("Masking text items needs the sheet they came from.")
        kept, _dropped = apply_mask_to_items(subject, mask, sheet)
        return kept

    if isinstance(subject, list):
        if sheet is None:
            raise ValidationError("Masking geometry needs the sheet it came from.")
        width, height = sheet.width_px, sheet.height_px
        kept = []
        for entry in subject:
            centre = _centre_px_of(entry)
            if centre is None:
                kept.append(entry)
                continue
            fx = centre[0] / width if width else 0.0
            fy = centre[1] / height if height else 0.0
            if not mask.covers(fx, fy):
                kept.append(entry)
        return kept

    raise ValidationError("That kind of item cannot be masked.")


def _centre_px_of(entry: Any) -> tuple[float, float] | None:
    centre = getattr(entry, "centre_px", None)
    if centre is not None:
        return float(centre[0]), float(centre[1])
    bbox = getattr(entry, "bbox", None)
    if bbox is not None and hasattr(bbox, "cx"):
        return float(bbox.cx), float(bbox.cy)
    return None


def invert_mask(mask: MaskSet, image: np.ndarray, fill: int = 255) -> np.ndarray:
    """The debug view: only what is being excluded."""
    return apply_mask_to_image(image, mask, fill=fill, invert=True)


# ── Persistence ─────────────────────────────────────────────────────────


def mask_profile_dir() -> Path:
    directory = get_app_paths().profiles / "masks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_to_profile(mask: MaskSet, *, profile_id: str, directory: Path | None = None) -> Path:
    """Write a mask so every sheet on the same template reuses it."""
    if not profile_id:
        raise ValidationError("A mask needs a profile name before it can be saved.")
    target_dir = directory or mask_profile_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{profile_id}.mask.json"
    target.write_text(json.dumps(mask.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Saved mask | profile={} | zones={}", profile_id, len(mask.zones))
    return target


def load_from_profile(profile_id: str, directory: Path | None = None) -> MaskSet:
    source = (directory or mask_profile_dir()) / f"{profile_id}.mask.json"
    if not source.exists():
        raise NotFoundError(f"No saved mask called '{profile_id}'.")
    data = json.loads(source.read_text(encoding="utf-8"))
    return MaskSet.from_dict(data)


def list_profiles(directory: Path | None = None) -> list[str]:
    target_dir = directory or mask_profile_dir()
    if not target_dir.exists():
        return []
    return sorted(path.name.removesuffix(".mask.json") for path in target_dir.glob("*.mask.json"))
