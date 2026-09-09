"""Unique text anchors: Method 1 of the Phase 4 alignment cascade.

The idea, from B4: text extraction already exists, so the cheapest possible
way to align two revisions of a CAD sheet is to reuse it. Each text string
that appears **exactly once** on the old sheet and **exactly once** on the
new sheet is an unambiguous correspondence — its centre on the old sheet maps
to its centre on the new.

Two design rules from the plan shape this module:

* **The title block is excluded.** Project name, client and address sit in
  the same place on every sheet of every revision, so they match perfectly
  no matter what happened to the drawing. Include them and you align the
  *sheet frame*, not the drawing content — and when the content moved while
  the frame did not, that is exactly the wrong answer, and it looks
  convincing. The zone exclusion mirrors the fingerprint module: any item
  inside a non-whole-sheet zone that scores as title-block-like is dropped
  before anything else happens.
* **Repetition is ambiguity.** A room label that appears four times cannot
  tell us which old room maps to which new room. Discarding the repeat is
  cheaper and safer than guessing — fewer anchors are better than wrong
  anchors. Multi-word strings are fine: a drawing-office label like
  ``RM-01 3000`` is exactly the kind of stable, unique anchor we want.

All coordinates are image pixels (y down) because that is the space the
raster pipeline and the transform fitting work in. Text extraction reports
PDF user-space points (y up) with an arbitrary page-box origin, so the two
conversions — origin out of the picture, y axis flipped — happen here and
nowhere else.
"""

from __future__ import annotations

import math
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from scipy.spatial import ConvexHull, QhullError

from engine.align.types import Anchor, Correspondence
from engine.extract.text_extractor import PageText, TextItem
from engine.titleblock.zone_detector import ZoneName, detect_zones

#: Points per inch — PDF user space.
POINTS_PER_INCH = 72.0

#: Anchor text must be at least this many characters after whitespace collapse.
MIN_ANCHOR_LENGTH = 2


# ── Geometry ─────────────────────────────────────────────────────────────


def page_points_to_image_px(
    x_pt: float,
    y_pt: float,
    box_x0: float,
    box_y0: float,
    width_pt: float,
    height_pt: float,
    dpi: float,
) -> tuple[float, float]:
    """Map PDF user-space points (y up) to render pixels (y down).

    PDF text coordinates are measured from the page box origin, which is not
    necessarily ``(0, 0)`` — the Lami Architects fixture has a media box from
    (-1192, -842) to (1192, 842). Both the origin offset and the y flip must
    be applied so that a text run lands on the same render pixel no matter
    where the page box started.
    """
    scale = dpi / POINTS_PER_INCH
    px = (x_pt - box_x0) * scale
    py = height_pt * scale - (y_pt - box_y0) * scale
    return px, py


def _item_centre_px(item: TextItem, page: PageText, dpi: float) -> tuple[float, float]:
    """Centre of one text run in render pixels."""
    centre_x_pt = item.x + item.width / 2.0
    centre_y_pt = item.y + item.height / 2.0
    return page_points_to_image_px(
        centre_x_pt,
        centre_y_pt,
        page.box.x0,
        page.box.y0,
        page.box.width,
        page.box.height,
        dpi,
    )


# ── Text filtering ───────────────────────────────────────────────────────


def _outside_title_zones(items: list[TextItem], page: PageText) -> list[TextItem]:
    """Items that sit outside the title block strip(s).

    Identical policy to the fingerprint module: every candidate zone that is
    not the whole sheet and carries title-block vocabulary contributes its
    items to the exclusion set. Zones nest (bottom-right is inside right and
    bottom), so exclusion is by item identity, not by zone arithmetic.
    """
    excluded: set[int] = set()
    for zone in detect_zones(page):
        if zone.name is ZoneName.WHOLE_SHEET or zone.score <= 0:
            continue
        excluded.update(id(item) for item in zone.items)
    return [item for item in items if id(item) not in excluded]


def _is_anchor_text(text: str) -> bool:
    """A string worth anchoring on.

    Rejects single characters (grid letters, dimension scraps), pure
    punctuation such as drawing separators (``-----``) and symbol dust.
    A string is usable when it is long enough and carries at least one
    letter or digit — either alone makes it meaning-bearing.
    """
    if len(text) < MIN_ANCHOR_LENGTH:
        return False
    if not any(character.isalpha() or character.isdigit() for character in text):
        return False
    meaningful = [character for character in text if not character.isspace()]
    return not all(unicodedata.category(character).startswith("P") for character in meaningful)


def normalise_anchor_text(text: str) -> str:
    """Comparison form for anchor matching: NFKC, upper case, one space runs.

    ``"rm-01\\u00a03000"`` and ``"RM-01 3000"`` must match, while the display
    text is left untouched — this form exists only so identical strings can
    be counted and matched.
    """
    return " ".join(unicodedata.normalize("NFKC", text).upper().split())


# ── Extraction ───────────────────────────────────────────────────────────


def extract_text_anchors(page: PageText, dpi: int = 200) -> list[Anchor]:
    """Turn one extracted page into unambiguous anchor points.

    Pipeline: drop title-block text, drop strings too short or punctuation-
    only, normalise the rest, then keep only strings that appear exactly once
    on the sheet. Each survivor becomes an :class:`Anchor` at the centre of
    its text run, with the run's pixel size carried so its weight reflects
    how reliable a correspondence it is.

    No PDF IO happens here: the caller already extracted ``page``.
    """
    scale = dpi / POINTS_PER_INCH
    counts: Counter[str] = Counter()
    candidates: list[tuple[str, TextItem]] = []

    for item in _outside_title_zones(page.items, page):
        cleaned = item.clean
        if not _is_anchor_text(cleaned):
            continue
        normalised = normalise_anchor_text(cleaned)
        candidates.append((normalised, item))
        counts[normalised] += 1

    anchors: list[Anchor] = []
    for normalised, item in candidates:
        if counts[normalised] != 1:
            # Repeats are ambiguous: which old copy maps to which new one?
            continue
        x_px, y_px = _item_centre_px(item, page, dpi)
        anchors.append(
            Anchor(
                text=normalised,
                x=x_px,
                y=y_px,
                width=item.width * scale,
                height=item.height * scale,
                source="text",
            )
        )
    anchors.sort(key=lambda anchor: anchor.text)
    return anchors


# ── Matching ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CorrespondenceResult:
    """Outcome of matching two anchor sets, with the numbers for logs."""

    correspondences: list[Correspondence] = field(default_factory=list)
    #: Total anchors found on each sheet, before ambiguity was considered.
    old_total: int = 0
    new_total: int = 0
    #: Number of text strings that matched exactly once on both sides.
    unique_matches: int = 0
    #: Distinct text strings dropped because they repeated on either side.
    discarded_ambiguous: int = 0


def _singletons(anchors: list[Anchor]) -> dict[str, Anchor]:
    """Text -> anchor for strings appearing exactly once; repeats dropped."""
    counts = Counter(anchor.text for anchor in anchors)
    return {anchor.text: anchor for anchor in anchors if counts[anchor.text] == 1}


def find_correspondences(
    old_anchors: list[Anchor], new_anchors: list[Anchor]
) -> CorrespondenceResult:
    """Match anchors by identical normalised text.

    Only strings present exactly once on *each* side can correspond; a repeat
    on either side is discarded as ambiguous. Weights come from the old
    anchor (``old.weight``): both copies of the label were drawn at the same
    size, so either would do, and the old sheet is the one being warped —
    that keeps the weight an intrinsic property of the source anchor.
    """
    old_singles = _singletons(old_anchors)
    new_singles = _singletons(new_anchors)

    old_counts = Counter(anchor.text for anchor in old_anchors)
    new_counts = Counter(anchor.text for anchor in new_anchors)
    ambiguous = sorted(
        text
        for text in old_counts.keys() | new_counts.keys()
        if old_counts[text] > 1 or new_counts[text] > 1
    )

    correspondences: list[Correspondence] = []
    for text in sorted(old_singles.keys() & new_singles.keys()):
        old_anchor = old_singles[text]
        new_anchor = new_singles[text]
        correspondences.append(
            Correspondence(
                old_x=old_anchor.x,
                old_y=old_anchor.y,
                new_x=new_anchor.x,
                new_y=new_anchor.y,
                weight=old_anchor.weight,
                label=text,
            )
        )

    return CorrespondenceResult(
        correspondences=correspondences,
        old_total=len(old_anchors),
        new_total=len(new_anchors),
        unique_matches=len(correspondences),
        discarded_ambiguous=len(ambiguous),
    )


# ── Quality of the anchor set ────────────────────────────────────────────


@dataclass(slots=True)
class AnchorQuality:
    """How well an anchor set spans the sheet (quality-gate metric 4).

    Anchors clustered in one corner fit beautifully there and are guesses
    everywhere else — the residual says "perfect" while the far corner of the
    sheet is misaligned by metres. Spread is the convex hull area of the old
    anchor points as a fraction of the page area.
    """

    count: int = 0
    spread_fraction: float = 0.0
    #: Nearest-neighbour distance among old points: min and max over anchors.
    min_spacing_px: float = 0.0
    max_spacing_px: float = 0.0


def _convex_hull_area(points: list[tuple[float, float]]) -> float:
    """Area in px^2 of the convex hull; 0 when fewer than 3 distinct points."""
    if len(points) < 3:
        return 0.0
    # Round before deduplicating: exact repeats (and near repeats) confuse
    # qhull, while a 1e-4 px shift cannot move the area measurably.
    unique = {(round(float(x), 4), round(float(y), 4)) for x, y in points}
    if len(unique) < 3:
        return 0.0
    array = list(unique)
    try:
        hull = ConvexHull(array)
    except QhullError:
        # Collinear points: no area, and that is the honest answer.
        return 0.0
    vertices = hull.vertices
    area = 0.0
    for index, vertex in enumerate(vertices):
        x1, y1 = array[vertex]
        x2, y2 = array[vertices[(index + 1) % len(vertices)]]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _nearest_neighbour_distances(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    """(min, max) nearest-neighbour distance over the old anchor points.

    Each anchor gets the distance to its closest sibling; the pair of
    extremes summarises how tightly the set is packed. With fewer than two
    points there is no spacing at all, reported as (0, 0).
    """
    if len(points) < 2:
        return 0.0, 0.0
    minimums: list[float] = []
    for index, (x1, y1) in enumerate(points):
        best = min(math.hypot(x1 - x2, y1 - y2) for j, (x2, y2) in enumerate(points) if j != index)
        minimums.append(best)
    return min(minimums), max(minimums)


def assess_anchor_quality(
    correspondences: list[Correspondence],
    page_size_px: tuple[float, float],
) -> AnchorQuality:
    """Summary of how well the correspondences span the (old) sheet.

    Uses the old-side anchor points — the sheet being warped. ``page_size_px``
    is that sheet's pixel size ``(width, height)``.
    """
    old_points = [
        (correspondence.old_x, correspondence.old_y) for correspondence in correspondences
    ]
    page_area = page_size_px[0] * page_size_px[1]
    spread = _convex_hull_area(old_points) / page_area if page_area > 0 else 0.0
    min_spacing, max_spacing = _nearest_neighbour_distances(old_points)
    return AnchorQuality(
        count=len(correspondences),
        spread_fraction=spread,
        min_spacing_px=min_spacing,
        max_spacing_px=max_spacing,
    )
