"""Vocabulary shared by the database, the API and the UI.

These are stored as plain strings in SQLite so the database stays readable in
any SQLite viewer, and so adding a value later does not need a migration.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Which issue a file belongs to."""

    OLD = "old"  # previous issue
    NEW = "new"  # current issue


class PairStatus(StrEnum):
    """Set reconciliation result for one sheet. Set before any comparison runs."""

    MATCHED = "matched"
    NEW = "new"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    DUPLICATE = "duplicate"


class MatchMethod(StrEnum):
    """How a pair was matched. Ordered from most to least trustworthy."""

    TITLE_BLOCK = "title_block"
    NORMALISED_NAME = "normalised_name"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class ChangeType(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MOVED = "moved"
    MODIFIED = "modified"
    COSMETIC = "cosmetic"


class Severity(StrEnum):
    """Ranked by likely cost impact, not by pixel area."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"


class UserStatus(StrEnum):
    """Triage state a user sets on a change."""

    OPEN = "open"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class TextChangeKind(StrEnum):
    DIMENSION = "dimension"
    TAG = "tag"
    NOTE = "note"


class AlignmentMethod(StrEnum):
    GRID_BUBBLE = "grid_bubble"
    FEATURE = "feature"
    TITLE_BLOCK = "title_block"
    IDENTITY = "identity"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AiMode(StrEnum):
    """Chosen by the user in Settings. Offline is the default."""

    OFFLINE = "offline"
    LOCAL = "local"
    CLOUD = "cloud"
