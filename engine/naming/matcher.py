"""Pair every old drawing with its new version, even when the names changed.

Five tiers, run in order, each removing matched pairs from the pool before the
next tier starts:

1. exact drawing number from the title block      confidence 1.00
2. normalised drawing number (MEDIUM clean)       confidence 0.98
3. normalised file name (MEDIUM, then AGGRESSIVE) confidence 0.92
4. token-set fuzzy file name match (rapidfuzz)    confidence = score / 100
5. content fingerprint similarity                 confidence = similarity

**Tiers 4 and 5 are a global assignment problem, not a per-file greedy loop.**
A greedy loop gives old drawing X its best-scoring candidate and moves on;
when old Y scores even higher against that same candidate, greedy has already
given the pair away. The correct answer comes from building the score matrix
and solving it once with ``scipy.optimize.linear_sum_assignment``.

The threshold discipline comes from the Phase 3 plan and it is strict:

* 0.90 and above  ->  auto-accept
* 0.70 to 0.90    ->  propose, but the user confirms before it is used
* below 0.70      ->  leave unmatched

A wrong pair is far worse than no pair: a wrong pair feeds Phase 4 a
comparison of two unrelated drawings and the user stops trusting the tool. An
unmatched drawing just asks a question.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from engine.core.models import SheetRecord
from engine.naming.fingerprint import SheetFingerprint, similarity
from engine.naming.normaliser import NormLevel, normalise
from engine.register.revision_logic import parse_revision

#: Tier identifiers, recorded on every pair and shown to the user.
TIER_EXACT = "exact_number"
TIER_NORMALISED_NUMBER = "normalised_number"
TIER_NORMALISED_NAME = "normalised_name"
TIER_FUZZY = "fuzzy_name"
TIER_FINGERPRINT = "content_fingerprint"
TIER_USER = "user"  # never produced here; recorded when the user pairs manually

#: Default assignment/review thresholds from the plan.
AUTO_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.70
#: How close a runner-up must be before a match is ambiguous.
AMBIGUITY_BAND = 0.05

#: Over this many old x new cells, solve the matrix in prefix blocks.
BLOCK_CELLS = 1500 * 1500

#: Tier metadata: which clean level its filename key uses and its confidence.
_NUMBER_TIERS: tuple[tuple[str, float], ...] = (
    (TIER_EXACT, 1.0),
    (TIER_NORMALISED_NUMBER, 0.98),
)


@dataclass(slots=True)
class MatchConfig:
    """Thresholds and knobs for one matching run."""

    auto_threshold: float = AUTO_THRESHOLD
    review_threshold: float = REVIEW_THRESHOLD
    ambiguity_band: float = AMBIGUITY_BAND
    block_cells: int = BLOCK_CELLS


@dataclass(slots=True)
class MatchCandidate:
    """An alternative partner the user might choose instead."""

    sheet: SheetRecord
    confidence: float
    tier: str
    reason: str


@dataclass(slots=True)
class MatchPair:
    """One old drawing paired with its new version."""

    old: SheetRecord
    new: SheetRecord
    confidence: float
    tier: str
    reason: str
    #: True when a second candidate sits within the ambiguity band.
    ambiguous: bool = False
    alternatives: list[MatchCandidate] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.confidence < AUTO_THRESHOLD or self.ambiguous


@dataclass(slots=True)
class MatchResult:
    """The outcome of matching two issue sets."""

    pairs: list[MatchPair] = field(default_factory=list)
    old_unmatched: list[SheetRecord] = field(default_factory=list)
    new_unmatched: list[SheetRecord] = field(default_factory=list)
    #: Duplicate files excluded before matching (same number, lower revision).
    superseded: list[SheetRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def auto_pairs(self) -> list[MatchPair]:
        return [pair for pair in self.pairs if not pair.needs_review]

    @property
    def review_pairs(self) -> list[MatchPair]:
        return [pair for pair in self.pairs if pair.needs_review]

    def summary(self) -> dict[str, int]:
        return {
            "auto": len(self.auto_pairs),
            "review": len(self.review_pairs),
            "old_unmatched": len(self.old_unmatched),
            "new_unmatched": len(self.new_unmatched),
        }


def _identity(sheet: SheetRecord) -> tuple[str, int]:
    """Stable identity of a sheet, for tracking across tiers."""
    return (sheet.abs_path, sheet.page_index)


def _number_key(sheet: SheetRecord) -> str:
    return sheet.normalised_no or normalise(sheet.drawing_no or "", NormLevel.MEDIUM).value


def _representatives(sheets: Sequence[SheetRecord]) -> tuple[list[SheetRecord], list[SheetRecord]]:
    """Collapse same-number duplicates to their newest representative.

    A folder often holds both the current sheet and the file it superseded.
    Matching both would create a false duplicate pair, so within a side each
    drawing number is represented once — by the highest revision, with a
    title-block number preferred over one read from the file name. Sheets with
    no number at all cannot be grouped and stay in the pool.
    """
    groups: dict[str, list[SheetRecord]] = {}
    order: list[str] = []
    representatives: list[SheetRecord] = []
    superseded: list[SheetRecord] = []

    for sheet in sheets:
        key = _number_key(sheet)
        if not key:
            representatives.append(sheet)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(sheet)

    source_rank = {"titleblock": 3, "drawing_list": 2, "user": 4, "sheet_text": 1, "filename": 0}

    for key in order:
        members = groups[key]
        if len(members) == 1:
            representatives.append(members[0])
            continue

        def rank(sheet: SheetRecord) -> tuple[int, int, str]:
            return (
                source_rank.get(sheet.source_of_number, 0),
                parse_revision(sheet.revision).ordinal,
                sheet.abs_path.lower(),
            )

        members_sorted = sorted(members, key=rank, reverse=True)
        representatives.append(members_sorted[0])
        superseded.extend(members_sorted[1:])

    return representatives, superseded


def _match_by_number(
    old: SheetRecord, new_pool: list[SheetRecord], *, exact: bool
) -> MatchPair | None:
    """Tier 1 (exact string) or tier 2 (normalised number) matching."""
    if exact:
        key = old.drawing_no or ""
        candidates = [sheet for sheet in new_pool if sheet.drawing_no == key]
        confidence, tier, reason = 1.0, TIER_EXACT, f"the drawing number {key} matches exactly"
    else:
        key = _number_key(old)
        candidates = [sheet for sheet in new_pool if _number_key(sheet) == key]
        confidence, tier, reason = (
            0.98,
            TIER_NORMALISED_NUMBER,
            ("the drawing numbers match apart from formatting"),
        )
    if not key or not candidates:
        return None
    if len(candidates) > 1:
        return None  # ambiguous within a side: never guess; leave to the user
    return MatchPair(
        old=old,
        new=candidates[0],
        confidence=confidence,
        tier=tier,
        reason=reason,
    )


def _tier3_name_match(
    old: SheetRecord, new_pool: list[SheetRecord], lookup: dict[str, list[SheetRecord]]
) -> MatchPair | None:
    medium = normalise(old.filename, NormLevel.MEDIUM)
    candidates = lookup.get(medium.value, [])
    tier = TIER_NORMALISED_NAME
    if len(candidates) == 1:
        return MatchPair(
            old=old,
            new=candidates[0],
            confidence=0.92,
            tier=tier,
            reason=f"file names match after removing {medium.reason()}",
        )
    aggressive = normalise(old.filename, NormLevel.AGGRESSIVE)
    candidates = lookup.get(f"a:{aggressive.value}", [])
    if len(candidates) == 1:
        return MatchPair(
            old=old,
            new=candidates[0],
            confidence=0.92,
            tier=tier,
            reason=f"file names match after {aggressive.reason()}",
        )
    return None


def _assign(
    scores: np.ndarray,
    old_sheets: list[SheetRecord],
    new_sheets: list[SheetRecord],
    *,
    tier: str,
    config: MatchConfig,
    reason_for: Callable[[SheetRecord, SheetRecord], str],
) -> list[MatchPair]:
    """Global optimal assignment over a score matrix.

    Scores below ``review_threshold`` are not candidates at all. The cost
    matrix is padded so that "no match" (cost 0) always beats a forced weak
    pair (positive cost), which is what makes an unmatched drawing an honest
    answer rather than a wrong pair.
    """
    rows, cols = scores.shape
    if rows == 0 or cols == 0:
        return []

    size = rows + cols
    cost = np.zeros((size, size), dtype=float)
    for row in range(rows):
        for col in range(cols):
            cost[row, col] = config.review_threshold - float(scores[row, col])

    row_index, col_index = linear_sum_assignment(cost)
    pairs: list[MatchPair] = []
    for row, col in zip(row_index, col_index, strict=True):
        if row >= rows or col >= cols:
            continue  # a padding pair: this old sheet stays unmatched
        score = float(scores[row, col])
        if score < config.review_threshold:
            continue
        pairs.append(
            _pair_with_alternatives(
                row,
                col,
                scores,
                old_sheets,
                new_sheets,
                tier=tier,
                config=config,
                reason_for=reason_for,
            )
        )
    return pairs


def _pair_with_alternatives(
    row: int,
    col: int,
    scores: np.ndarray,
    old_sheets: list[SheetRecord],
    new_sheets: list[SheetRecord],
    *,
    tier: str,
    config: MatchConfig,
    reason_for: Callable[[SheetRecord, SheetRecord], str],
) -> MatchPair:
    """One pair plus any near-tie alternatives for the review screen."""
    score = float(scores[row, col])
    old_sheet = old_sheets[row]

    runner_up: float = 0.0
    for other in range(len(new_sheets)):
        if other == col:
            continue
        runner_up = max(runner_up, float(scores[row, other]))

    pair = MatchPair(
        old=old_sheet,
        new=new_sheets[col],
        confidence=round(score, 3),
        tier=tier,
        reason=reason_for(old_sheet, new_sheets[col]),
        ambiguous=runner_up >= config.review_threshold
        and score - runner_up <= config.ambiguity_band,
    )

    ranked = sorted(
        ((float(scores[row, other]), other) for other in range(len(new_sheets)) if other != col),
        reverse=True,
    )
    for alt_score, alt_col in ranked[:3]:
        if alt_score < config.review_threshold:
            break
        pair.alternatives.append(
            MatchCandidate(
                sheet=new_sheets[alt_col],
                confidence=round(alt_score, 3),
                tier=tier,
                reason=reason_for(old_sheet, new_sheets[alt_col]),
            )
        )
    return pair


def _block_rows(old_sheets: list[SheetRecord]) -> list[list[int]]:
    """Group row indices by filename prefix so huge matrices stay solvable."""
    buckets: dict[str, list[int]] = {}
    for row, sheet in enumerate(old_sheets):
        prefix = normalise(sheet.filename, NormLevel.AGGRESSIVE).value[:5]
        if not prefix:
            prefix = _number_key(sheet)[:5] or "?"
        buckets.setdefault(prefix, []).append(row)
    return list(buckets.values())


def match_sets(
    old_sheets: Sequence[SheetRecord],
    new_sheets: Sequence[SheetRecord],
    config: MatchConfig | None = None,
    *,
    fingerprint_for: Callable[[SheetRecord], SheetFingerprint | None] | None = None,
) -> MatchResult:
    """Match two issue sets through the five tiers.

    `fingerprint_for` supplies a cached fingerprint for a sheet — the matcher
    itself never opens a PDF. When it is None, tier 5 is skipped rather than
    guessed.
    """
    config = config or MatchConfig()
    result = MatchResult()

    old_pool, old_superseded = _representatives(old_sheets)
    new_pool, new_superseded = _representatives(new_sheets)
    result.superseded = [*old_superseded, *new_superseded]
    if old_superseded:
        result.notes.append(
            f"{len(old_superseded)} older sheet(s) with a duplicate number on the old side "
            "were not matched again."
        )
    if new_superseded:
        result.notes.append(
            f"{len(new_superseded)} older sheet(s) with a duplicate number on the new side "
            "were not matched again."
        )

    # -- Tiers 1 and 2: the drawing number ------------------------------
    for tier, _confidence in _NUMBER_TIERS:
        exact = tier == TIER_EXACT
        remaining: list[SheetRecord] = []
        for old in old_pool:
            pair = _match_by_number(old, new_pool, exact=exact)
            if pair is None:
                remaining.append(old)
                continue
            result.pairs.append(pair)
            new_pool = [sheet for sheet in new_pool if sheet is not pair.new]
        old_pool = remaining

    # -- Tier 3: the normalised file name -------------------------------
    medium_lookup: dict[str, list[SheetRecord]] = {}
    aggressive_lookup: dict[str, list[SheetRecord]] = {}
    for sheet in new_pool:
        medium_lookup.setdefault(normalise(sheet.filename, NormLevel.MEDIUM).value, []).append(
            sheet
        )
        aggressive_lookup.setdefault(
            f"a:{normalise(sheet.filename, NormLevel.AGGRESSIVE).value}", []
        ).append(sheet)

    remaining: list[SheetRecord] = []
    for old in old_pool:
        pair = _tier3_name_match(old, new_pool, {**medium_lookup, **aggressive_lookup})
        if pair is None:
            remaining.append(old)
            continue
        result.pairs.append(pair)
        new_pool = [sheet for sheet in new_pool if sheet is not pair.new]
    old_pool = remaining

    # -- Tiers 4 and 5: fuzzy names, then fingerprints ------------------
    for tier, score_fn, reason_for in (
        (
            TIER_FUZZY,
            lambda left, right: fuzz.token_set_ratio(left.filename, right.filename) / 100.0,
            lambda left, right: f"the file names look alike ({left.filename} / {right.filename})",
        ),
        (
            TIER_FINGERPRINT,
            lambda left, right: _fingerprint_score(left, right, fingerprint_for),
            lambda left, right: "the sheet content matches (fingerprint)",
        ),
    ):
        if not old_pool or not new_pool:
            break
        if tier == TIER_FINGERPRINT and fingerprint_for is None:
            break

        rows, cols = len(old_pool), len(new_pool)
        if rows * cols > config.block_cells:
            row_blocks = _block_rows(old_pool)
        else:
            row_blocks = [list(range(rows))]

        tier_pairs: list[MatchPair] = []
        for row_block in row_blocks:
            scores = np.zeros((len(row_block), cols), dtype=float)
            for local, row in enumerate(row_block):
                for col in range(cols):
                    score = score_fn(old_pool[row], new_pool[col])
                    if score >= config.review_threshold:
                        scores[local, col] = score
            tier_pairs.extend(
                _assign(
                    scores,
                    [old_pool[row] for row in row_block],
                    new_pool,
                    tier=tier,
                    config=config,
                    reason_for=reason_for,
                )
            )

        matched_new: set[tuple[str, int]] = set()
        for pair in tier_pairs:
            result.pairs.append(pair)
            matched_new.add(_identity(pair.new))
        if matched_new:
            new_pool = [sheet for sheet in new_pool if _identity(sheet) not in matched_new]
        matched_old = {_identity(pair.old) for pair in tier_pairs}
        old_pool = [sheet for sheet in old_pool if _identity(sheet) not in matched_old]

    result.old_unmatched = old_pool
    result.new_unmatched = new_pool
    result.pairs.sort(key=lambda pair: (_identity(pair.old), _identity(pair.new)))
    return result


def _fingerprint_score(
    left: SheetRecord,
    right: SheetRecord,
    fingerprint_for: Callable[[SheetRecord], SheetFingerprint | None] | None,
) -> float:
    if fingerprint_for is None:
        return 0.0
    left_fp = fingerprint_for(left)
    right_fp = fingerprint_for(right)
    if left_fp is None or right_fp is None:
        return 0.0
    return similarity(left_fp, right_fp)
