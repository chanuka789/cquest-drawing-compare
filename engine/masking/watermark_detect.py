"""Task 5.3 — watermarks, stamps and post-issue annotations.

Everything here was added *after* the drawing was designed: a `PRELIMINARY`
stamped across the sheet, a document controller's received stamp, a QR code,
a digital signature block, a reviewer's Bluebeam comment. None of it is a
design change, and all of it differs between two issues.

Nothing is applied silently. Each detection carries a confidence, and
anything below :data:`CONFIDENCE_TO_APPLY` is *proposed* in the mask editor
rather than excluded on its own authority — the cost of wrongly masking a
region is that a real change disappears, which is exactly the failure this
phase exists to avoid.

A note on rotation. pdfium reports an axis-aligned box per text run, not an
angle, so a 45-degree watermark is recognised by its *shape*: a horizontal
string of N characters is about ``0.55 * size * N`` wide and one size tall,
while the same string set at 45 degrees has a box nearly as tall as it is
wide. That ratio is a more reliable signal than any single threshold on size.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from loguru import logger

from engine.extract.text_extractor import TextItem
from engine.masking.types import FracRect, SheetView, Zone, ZoneType

#: Below this a detection is proposed to the user, never applied on its own.
CONFIDENCE_TO_APPLY = 0.7

#: Configurable in the project profile; these are the defaults.
WATERMARK_WORDS: tuple[str, ...] = (
    "PRELIMINARY",
    "DRAFT",
    "SUPERSEDED",
    "NOT FOR CONSTRUCTION",
    "FOR APPROVAL",
    "FOR INFORMATION",
    "FOR TENDER",
    "VOID",
    "COPY",
    "UNCONTROLLED",
    "CONFIDENTIAL",
    "SAMPLE",
)

SIGNATURE_KEYWORDS: tuple[str, ...] = (
    "DIGITALLY SIGNED",
    "SIGNATURE",
    "SIGNED BY",
    "VERIFIED",
    "ELECTRONICALLY SIGNED",
    "DOCUSIGN",
)

STAMP_KEYWORDS: tuple[str, ...] = (
    "RECEIVED",
    "APPROVED FOR CONSTRUCTION",
    "REVIEWED",
    "NO OBJECTION",
    "STATUS A",
    "STATUS B",
    "STATUS C",
    "AS BUILT",
)

#: Font size above this multiple of the sheet median counts as "huge".
HUGE_FONT_MULTIPLE = 3.0
#: A box spanning more than this fraction of a page dimension counts as wide.
WIDE_SPAN_FRACTION = 0.40
#: Mean ink this light (0 black, 255 white) counts as grey rather than solid.
GREY_INK_THRESHOLD = 110


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).upper().split())


@dataclass(slots=True)
class Detection:
    """One thing found on the sheet that should not be compared."""

    kind: str
    rect: FracRect
    text: str = ""
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    #: True when the ink is pale enough to remove by value rather than area.
    pale_ink: bool = False
    #: The grey value separating this detection's ink from the drawing's.
    ink_threshold: int = 130
    #: The watermark phrase itself, when one was recognised. pdfium reports
    #: everything geometrically inside a rotated run's box as part of it, so
    #: the run text can carry real drawing labels; the phrase does not.
    match_word: str = ""

    @property
    def applies_automatically(self) -> bool:
        return self.confidence >= CONFIDENCE_TO_APPLY

    def as_zone(self, zone_type: ZoneType) -> Zone:
        # A watermark's box covers a third of the sheet. Excluding that box
        # would hide a third of the drawing, so a pale watermark is excluded
        # by ink value instead and the drawing under it stays compared.
        ink_only = zone_type is ZoneType.WATERMARK and self.pale_ink
        return Zone(
            type=zone_type,
            rect=self.rect.clipped(),
            label=self.text or self.kind.replace("_", " ").title(),
            confidence=self.confidence,
            evidence=list(self.signals),
            enabled=self.applies_automatically,
            ink_only=ink_only,
            ink_threshold=self.ink_threshold,
            match_text=_normalise(self.match_word or self.text),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rect": self.rect.as_dict(),
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "signals": list(self.signals),
            "applies_automatically": self.applies_automatically,
        }


Watermark = Detection
Stamp = Detection


# ── Watermarks ──────────────────────────────────────────────────────────


#: A string must be this long before its box shape says anything about angle.
MIN_ROTATION_CHARS = 4
#: How far the measured aspect may sit from the horizontal or vertical ideal,
#: as a fraction of the log distance between them, before it reads as rotated.
ROTATION_MARGIN = 0.6


def _rotation_signal(item: TextItem) -> tuple[bool, float]:
    """Is this run rotated off the horizontal, and by how much?

    pdfium gives an axis-aligned box, never an angle, so the angle comes from
    the box's *shape*. A horizontal run of N characters is about ``0.55 * N``
    times wider than tall; the same run set vertically is the reciprocal of
    that; a run at 45 degrees is roughly square. Comparing the measured
    aspect against those two ideals in log space separates the three cases
    without a magic threshold on size.

    Note the trap this replaces: :attr:`TextItem.font_size` is *derived* from
    the box height, so "the box is taller than the font size" is true of
    nothing, ever. It silently disabled the rotation signal entirely.
    """
    text = item.clean
    characters = len(text.replace(" ", ""))
    if characters < MIN_ROTATION_CHARS or item.width <= 0 or item.height <= 0:
        return False, 0.0

    aspect = item.width / item.height
    horizontal_ideal = 0.55 * characters
    if horizontal_ideal <= 1.2:
        return False, 0.0

    log_aspect = math.log(aspect)
    log_ideal = math.log(horizontal_ideal)
    if log_aspect > ROTATION_MARGIN * log_ideal:
        return False, 0.0  # laid out along the sheet
    if log_aspect < -ROTATION_MARGIN * log_ideal:
        return False, 90.0  # set vertically, which is normal on a drawing

    # For a long string the glyph height is negligible beside the run length,
    # so the box diagonal is the baseline and this angle is the real one.
    return True, math.degrees(math.atan2(item.height, item.width))


#: At least this share of the ink in the box must be pale before the
#: detection counts as a grey wash rather than solid drawing.
MIN_PALE_SHARE = 0.2
#: How far below the watermark's own tone the removal threshold sits.
TONE_MARGIN = 30
#: And never below this, or solid drawing ink starts being removed.
MIN_INK_THRESHOLD = 120


def _grey_fill(sheet: SheetView, rect: FracRect) -> tuple[bool, float]:
    """Is the ink inside *rect* grey or outlined rather than solid black?

    The tone is the **most common** pale ink value in the box, not the mean.
    A watermark's box also contains the drawing under it, and averaging the
    two produces a value between the watermark's grey and the drawing's
    black — which then sets a removal threshold that eats the drawing's own
    anti-aliased lines. A thin line rendered over the wash comes out darker
    than the same line on white, so the old sheet loses it and the new one
    keeps it, and the comparison reports ink that was there all along.
    """
    if sheet.gray is None:
        return False, 0.0
    x0, y0, x1, y1 = sheet.px_rect(rect)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return False, 0.0
    patch = sheet.gray[y0:y1, x0:x1]
    ink = patch[patch < 250]
    if ink.size == 0:
        return False, 0.0

    pale = ink[ink >= GREY_INK_THRESHOLD]
    if pale.size < ink.size * MIN_PALE_SHARE:
        # Mostly solid ink: a black stamp, or a box full of drawing.
        return False, float(ink.mean())
    tone = float(np.argmax(np.bincount(pale, minlength=256)))
    return True, tone


def detect_watermarks(
    sheet: SheetView, words: tuple[str, ...] = WATERMARK_WORDS
) -> list[Watermark]:
    """Large, rotated, pale or known-phrase text laid across the drawing.

    Any two of the five signals is enough. One alone is not: a drawing title
    is large, a rotated dimension is rotated, and a grey hatch label is pale —
    but none of those is a watermark.
    """
    page = sheet.page_text
    if page is None or not page.items:
        return []

    sizes = [item.font_size for item in page.items if item.font_size > 0]
    median_size = float(np.median(sizes)) if sizes else 0.0

    found: list[Watermark] = []
    for item in page.items:
        text = _normalise(item.clean)
        if not text:
            continue
        rect = sheet.item_rect(item)
        signals: list[str] = []

        rotated, angle = _rotation_signal(item)
        if rotated:
            signals.append(f"Set at about {angle:.0f} degrees, not square to the sheet.")

        if median_size > 0 and item.font_size > median_size * HUGE_FONT_MULTIPLE:
            signals.append(
                f"Its text box is {item.font_size / median_size:.0f} times the height of "
                "normal text on this sheet."
            )

        if rect.width > WIDE_SPAN_FRACTION or rect.height > WIDE_SPAN_FRACTION:
            signals.append("Spans more than 40% of the sheet.")

        grey, tone = _grey_fill(sheet, rect)
        if grey:
            signals.append(f"Drawn in grey rather than solid black (ink tone {tone:.0f}).")

        matched_word = next((word for word in words if word in text), None)
        if matched_word:
            signals.append(f"Reads '{matched_word}', a known watermark phrase.")

        # Size and span alone describe a drawing title as well as a watermark,
        # and masking the drawing title would hide a real change. At least one
        # distinguishing signal — rotated, pale, or a known phrase — is
        # required before anything is excluded.
        distinguishing = rotated or grey or matched_word is not None
        if not distinguishing or len(signals) < 2:
            continue

        confidence = min(0.98, 0.45 + 0.18 * len(signals) + (0.15 if matched_word else 0.0))
        found.append(
            Detection(
                kind="watermark",
                rect=rect.expanded(0.005).clipped(),
                text=item.clean,
                confidence=confidence,
                signals=signals,
                pale_ink=grey,
                # Just below this watermark's own tone: everything paler is
                # the wash, everything darker is the drawing under it.
                ink_threshold=int(max(MIN_INK_THRESHOLD, tone - TONE_MARGIN)) if grey else 130,
                match_word=matched_word or "",
            )
        )

    logger.debug("Watermark detection | candidates={}", len(found))
    return found


# ── Stamps, QR codes and signature blocks ───────────────────────────────


def detect_stamps(sheet: SheetView) -> list[Stamp]:
    """QR codes, barcodes, signature blocks and document-control stamps."""
    found: list[Stamp] = []
    found.extend(_keyword_stamps(sheet, SIGNATURE_KEYWORDS, "signature_block"))
    found.extend(_keyword_stamps(sheet, STAMP_KEYWORDS, "control_stamp"))
    found.extend(detect_codes(sheet))
    return found


def _keyword_stamps(sheet: SheetView, keywords: tuple[str, ...], kind: str) -> list[Stamp]:
    page = sheet.page_text
    if page is None:
        return []
    found: list[Stamp] = []
    for item in page.items:
        text = _normalise(item.clean)
        matched = next((keyword for keyword in keywords if keyword in text), None)
        if matched is None:
            continue
        rect = sheet.item_rect(item).expanded(0.03)
        corner = _corner_score(rect)
        signed_bonus = 0.1 if kind == "signature_block" else 0.0
        found.append(
            Detection(
                kind=kind,
                rect=rect.clipped(),
                text=item.clean,
                confidence=min(0.95, 0.6 + 0.2 * corner + signed_bonus),
                signals=[
                    f"Reads '{matched}', which is added after issue, not designed.",
                    "Sits in a page corner." if corner > 0.5 else "Sits away from the drawing.",
                ],
            )
        )
    return found


def _corner_score(rect: FracRect) -> float:
    """1.0 in a page corner, 0.0 in the middle of the sheet."""
    cx, cy = rect.centre
    return max(abs(cx - 0.5), abs(cy - 0.5)) * 2.0


def detect_codes(sheet: SheetView) -> list[Stamp]:
    """QR codes and barcodes: added by document control, never designed."""
    if sheet.gray is None or sheet.gray.size == 0:
        return []

    height, width = sheet.gray.shape[:2]
    found: list[Stamp] = []
    try:
        detector = cv2.QRCodeDetector()
        ok, points = detector.detectMulti(sheet.gray)
    except cv2.error:  # pragma: no cover - OpenCV build without QR support
        ok, points = False, None

    if ok and points is not None:
        for quad in np.asarray(points).reshape(-1, 4, 2):
            rect = FracRect.from_points(
                [(float(x) / width, float(y) / height) for x, y in quad]
            ).expanded(0.004)
            found.append(
                Detection(
                    kind="qr_code",
                    rect=rect.clipped(),
                    text="QR code",
                    confidence=0.9,
                    signals=["A QR code was decoded here; document control added it."],
                )
            )

    found.extend(_barcode_candidates(sheet))
    return found


def _barcode_candidates(sheet: SheetView) -> list[Stamp]:
    """Dense runs of parallel bars in a page corner, with no QR decode.

    Deliberately conservative — a barcode looks a lot like a hatched wall, so
    only corner regions are considered and the result is proposed, not
    applied.
    """
    gray = sheet.gray
    if gray is None:
        return []
    height, width = gray.shape[:2]
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, max(9, height // 120)))
    bars = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(bars, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = [cv2.boundingRect(contour) for contour in contours]
    tall_thin = [
        box for box in boxes if box[3] > box[2] * 4 and box[3] > height * 0.01 and box[2] > 1
    ]
    if len(tall_thin) < 12:
        return []

    rect = FracRect.from_points(
        [(box[0] / width, box[1] / height) for box in tall_thin]
        + [((box[0] + box[2]) / width, (box[1] + box[3]) / height) for box in tall_thin]
    )
    if _corner_score(rect) < 0.55 or rect.area > 0.05:
        return []
    return [
        Detection(
            kind="barcode",
            rect=rect.expanded(0.005).clipped(),
            text="Barcode",
            confidence=0.6,
            signals=[
                f"{len(tall_thin)} parallel bars of even height in a page corner.",
                "Proposed rather than applied — confirm it is not hatching.",
            ],
        )
    ]


# ── Post-issue annotations ──────────────────────────────────────────────


def detect_annotations(
    sheet: SheetView, annotations_mask: np.ndarray | None, min_area_px: float = 40.0
) -> list[Detection]:
    """Regions covered by PDF annotations, from the Phase 4 render split.

    Phase 4 already renders each page twice and keeps the annotation paint as
    its own layer, so this needs no new PDF work: any pixel the annotation
    layer touched is comment, not design.
    """
    if annotations_mask is None or annotations_mask.size == 0:
        return []
    height, width = annotations_mask.shape[:2]
    binary = (annotations_mask > 0).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)

    found: list[Detection] = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < min_area_px:
            continue
        rect = FracRect(x / width, y / height, (x + w) / width, (y + h) / height)
        found.append(
            Detection(
                kind="annotation",
                rect=rect.expanded(0.002).clipped(),
                text="Annotation",
                confidence=0.95,
                signals=["Painted by a PDF annotation, so it was added after the issue."],
            )
        )
    return found


def detections_to_zones(detections: list[Detection]) -> list[Zone]:
    """Map detections onto mask zones of the right type."""
    mapping = {
        "watermark": ZoneType.WATERMARK,
        "qr_code": ZoneType.STAMP,
        "barcode": ZoneType.STAMP,
        "signature_block": ZoneType.STAMP,
        "control_stamp": ZoneType.STAMP,
        "annotation": ZoneType.ANNOTATION,
    }
    return [
        detection.as_zone(mapping.get(detection.kind, ZoneType.STAMP)) for detection in detections
    ]
