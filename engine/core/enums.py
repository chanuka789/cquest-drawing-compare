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


class IssueType(StrEnum):
    """Whether the current issue is the whole set or only what changed.

    This single answer changes the meaning of the whole register, so it is
    asked rather than assumed, and recorded in the audit log.
    """

    FULL = "full"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class RegisterStatus(StrEnum):
    """The reconciliation result for one drawing number."""

    REVISED = "revised"
    UNCHANGED = "unchanged"
    #: Same revision, different content. Someone reissued without bumping it.
    SAME_REV_DIFFERENT_FILE = "same_rev_different_file"
    NEW = "new"
    #: Old-only on a partial issue. Entirely normal, needs no action.
    NOT_REISSUED = "not_reissued"
    #: Old-only on a full issue. Possible scope deletion, needs confirmation.
    REMOVED = "removed"
    SUPERSEDED_IN_FOLDER = "superseded_in_folder"
    DUPLICATE_FILE = "duplicate_file"
    UNIDENTIFIED = "unidentified"
    UNREADABLE = "unreadable"
    IN_LIST_NOT_IN_FOLDER = "in_list_not_in_folder"
    IN_FOLDER_NOT_IN_LIST = "in_folder_not_in_list"
    STATUS_CHANGE = "status_change"


#: Statuses the user has to look at. Sorted to the top of the register.
NEEDS_ATTENTION: frozenset[RegisterStatus] = frozenset(
    {
        RegisterStatus.SAME_REV_DIFFERENT_FILE,
        RegisterStatus.UNIDENTIFIED,
        RegisterStatus.UNREADABLE,
        RegisterStatus.REMOVED,
        RegisterStatus.IN_LIST_NOT_IN_FOLDER,
    }
)


class AiMode(StrEnum):
    """Chosen by the user in Settings. Offline is the default."""

    OFFLINE = "offline"
    LOCAL = "local"
    CLOUD = "cloud"
