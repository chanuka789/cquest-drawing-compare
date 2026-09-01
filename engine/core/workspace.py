"""The output workspace: where everything the app produces is written.

One rule shapes this module: **the app never writes into the user's input
folders.** If the Excel register or a PDF overlay lands inside the "current
issue" folder, the next scan picks it up as a drawing and the folder starts
polluting itself. So the output folder is validated up front and refused if it
sits inside either input.

The other thing done up front is the **write test**. People point at read-only
network shares constantly. Finding out at the moment the folder is chosen is a
correction; finding out twenty minutes later when the export runs is a lost
afternoon.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from engine.utils.longpath import long_path

#: The folders every comparison workspace contains.
WORKSPACE_FOLDERS: tuple[str, ...] = (
    "01_Register",
    "02_Overlays",  # empty until Phase 6
    "03_Reports",
    "04_Renamed",  # empty until Phase 3
    "_audit",
)

REGISTER_FOLDER = "01_Register"
AUDIT_FOLDER = "_audit"
AUDIT_LOG_NAME = "run_log.json"
SCAN_CACHE_NAME = "scan_cache.db"

#: Warn below this much free space. A set of overlays is not small.
LOW_DISK_SPACE_BYTES = 500 * 1024 * 1024


@dataclass(slots=True)
class ValidationResult:
    """Whether a chosen output folder can actually be used."""

    path: str
    exists: bool = False
    is_writable: bool = False
    is_inside_input: bool = False
    is_same_as_input: bool = False
    is_empty: bool = True
    free_bytes: int = 0
    #: Problems that stop the run.
    errors: list[str] = field(default_factory=list)
    #: Things worth saying but which do not block anything.
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "is_writable": self.is_writable,
            "is_inside_input": self.is_inside_input,
            "is_same_as_input": self.is_same_as_input,
            "is_empty": self.is_empty,
            "free_bytes": self.free_bytes,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "is_valid": self.is_valid,
        }


def _normalise(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_inside(child: str | Path, parent: str | Path) -> bool:
    """True when *child* is the same as, or inside, *parent*."""
    child_path = _normalise(child)
    parent_path = _normalise(parent)
    if child_path == parent_path:
        return True
    return child_path.startswith(parent_path.rstrip("\\/") + os.sep)


def can_write_to(folder: str | Path) -> bool:
    """Test writability by actually writing, then cleaning up.

    Checking permission bits is not enough on a network share; the only
    reliable answer comes from trying.
    """
    target = Path(long_path(folder))
    probe = target / f".cqdc-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True


def validate_output_folder(
    path: str | Path,
    old_folder: str | Path | None = None,
    new_folder: str | Path | None = None,
) -> ValidationResult:
    """Check an output folder before anything is written to it."""
    target = Path(str(path))
    result = ValidationResult(path=str(target))

    for label, folder in (("previous issue", old_folder), ("current issue", new_folder)):
        if not folder:
            continue
        if _normalise(target) == _normalise(folder):
            result.is_same_as_input = True
            result.errors.append(
                f"This is the {label} folder. Choose a different folder for the "
                "output, so the results are never mixed in with the drawings."
            )
        elif is_inside(target, folder):
            result.is_inside_input = True
            result.errors.append(
                f"This folder is inside the {label} folder. Choose a folder "
                "outside it, or the next scan will read the results as drawings."
            )

    result.exists = target.is_dir()

    if result.exists:
        result.is_writable = can_write_to(target)
        if not result.is_writable:
            result.errors.append(
                "This folder is read-only. Choose a different folder, or ask for "
                "write access to this one."
            )
        try:
            result.is_empty = not any(target.iterdir())
        except OSError:
            result.is_empty = True
        if not result.is_empty:
            result.warnings.append(
                "This folder is not empty. Existing files are left alone and "
                "nothing is overwritten."
            )
    else:
        parent = target.parent
        if parent.is_dir():
            result.is_writable = can_write_to(parent)
            if result.is_writable:
                result.warnings.append("This folder does not exist yet and will be created.")
            else:
                result.errors.append(
                    f"The folder {parent} is read-only, so a new folder cannot be "
                    "created there. Choose a different location."
                )
        else:
            result.errors.append(
                "That location does not exist. Check the path, or the network "
                "drive may be disconnected."
            )

    try:
        probe = target if result.exists else target.parent
        result.free_bytes = shutil.disk_usage(str(probe)).free
        if result.free_bytes < LOW_DISK_SPACE_BYTES:
            result.warnings.append(
                f"Only {result.free_bytes / 1024 / 1024:.0f} MB free on this drive. "
                "Overlays and reports may not fit."
            )
    except OSError:
        result.free_bytes = 0

    return result


def suggest_output_folder(
    old_folder: str | Path,
    new_folder: str | Path,
    old_revision: str | None = None,
    new_revision: str | None = None,
    today: datetime | None = None,
) -> Path:
    """Propose an output folder, so the user can accept it with one click.

    Named for what it contains: `Compare_RevC_to_RevD_2026-09-01`.
    """
    stamp = (today or datetime.now(UTC)).strftime("%Y-%m-%d")

    if old_revision and new_revision:
        middle = f"Rev{_safe(old_revision)}_to_Rev{_safe(new_revision)}"
    else:
        middle = f"{_safe(Path(str(old_folder)).name)}_to_{_safe(Path(str(new_folder)).name)}"

    name = f"Compare_{middle}_{stamp}"

    # Sit alongside the two issue folders rather than inside either of them.
    parent = Path(str(new_folder)).parent
    if is_inside(parent, new_folder) or is_inside(parent, old_folder):
        parent = Path(str(new_folder)).parent.parent
    return parent / "Comparisons" / name


def _safe(text: str) -> str:
    """Make a fragment safe for a Windows folder name."""
    cleaned = "".join(character if character.isalnum() else "-" for character in text.strip())
    return "-".join(part for part in cleaned.split("-") if part)[:40] or "issue"


@dataclass(slots=True)
class Workspace:
    """A created output workspace and the paths inside it."""

    root: Path

    @property
    def register_dir(self) -> Path:
        return self.root / REGISTER_FOLDER

    @property
    def overlays_dir(self) -> Path:
        return self.root / "02_Overlays"

    @property
    def reports_dir(self) -> Path:
        return self.root / "03_Reports"

    @property
    def renamed_dir(self) -> Path:
        return self.root / "04_Renamed"

    @property
    def audit_dir(self) -> Path:
        return self.root / AUDIT_FOLDER

    @property
    def scan_cache_path(self) -> Path:
        return self.audit_dir / SCAN_CACHE_NAME

    @property
    def audit_log_path(self) -> Path:
        return self.audit_dir / AUDIT_LOG_NAME

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "register": str(self.register_dir),
            "overlays": str(self.overlays_dir),
            "reports": str(self.reports_dir),
            "renamed": str(self.renamed_dir),
            "audit": str(self.audit_dir),
        }


def create_workspace(path: str | Path) -> Workspace:
    """Build the workspace folder structure.

    Created as soon as the output folder is confirmed, so the user can see
    what is going to happen before anything runs.
    """
    root = Path(long_path(str(path)))
    root.mkdir(parents=True, exist_ok=True)
    for name in WORKSPACE_FOLDERS:
        (root / name).mkdir(exist_ok=True)

    logger.info("Workspace ready at {}", path)
    return Workspace(root=Path(str(path)))


def unique_path(target: str | Path) -> Path:
    """A path that does not exist yet, by appending a number if needed.

    An existing register is never overwritten silently: someone may already
    have sent it to the design team.
    """
    candidate = Path(str(target))
    if not candidate.exists():
        return candidate

    stem, suffix, parent = candidate.stem, candidate.suffix, candidate.parent
    for index in range(2, 1000):
        alternative = parent / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative

    raise FileExistsError(f"Could not find a free name for {target}")


def write_audit_log(workspace: Workspace, run_info: dict[str, object]) -> Path:
    """Record what was run, with what settings, when.

    This is what makes the output defensible if it ever supports a variation
    claim, so it records versions and settings, not just results.
    """
    from engine import __version__

    payload = {
        "app": "C-Quest Drawing Compare",
        "version": __version__,
        "written_at": datetime.now(UTC).isoformat(),
        **run_info,
    }

    workspace.audit_dir.mkdir(parents=True, exist_ok=True)
    target = workspace.audit_log_path
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    logger.info("Audit log written to {}", target)
    return target
