"""Generate the Phase 3 golden fixture folders, which live outside the repo.

Run as a module so pytest collection never triggers the build:

    python -m tests.build_phase3_fixtures

The fixtures are written under :data:`FIXTURE_ROOT` (default
``D:\\GitHub\\_cqdc-fixtures`` per CLAUDE.md, overridable with the
``CQDC_FIXTURE_ROOT`` environment variable):

* ``03_renamed\\{old,new}``  — the same drawings under two naming standards
* ``08_naming_mess\\new``    — every naming sin on one drawing number
* ``09_collision\\new``      — two different drawings whose cleaned names collide

All of the heavy work happens only when ``main()`` runs; importing this module
never writes a file.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.fixture_builder import (
    build_collision_folder,
    build_naming_mess,
    build_renamed_pair,
)

#: Where the golden fixtures live: outside the repository, per CLAUDE.md.
FIXTURE_ROOT = Path(os.environ.get("CQDC_FIXTURE_ROOT", r"D:\GitHub\_cqdc-fixtures"))


def _count_pdfs(folder: Path) -> int:
    return len(list(folder.glob("*.pdf")))


def build_all(root: Path | None = None) -> dict[str, Path]:
    """Create every Phase 3 fixture under *root* and report what was produced.

    Returns a map of fixture label to the folder that holds its PDFs. Re-runs
    overwrite the drawings in place; nothing is deleted.
    """
    base = Path(root) if root is not None else FIXTURE_ROOT
    base.mkdir(parents=True, exist_ok=True)

    old_dir, new_dir = build_renamed_pair(base / "03_renamed")
    mess_new = build_naming_mess(base / "08_naming_mess")
    collision_new = build_collision_folder(base / "09_collision")

    report = {
        "03_renamed/old (10 shared + 1 old-only)": old_dir,
        "03_renamed/new (10 shared + 1 new-only)": new_dir,
        "08_naming_mess/new": mess_new,
        "09_collision/new": collision_new,
    }
    for label, folder in report.items():
        print(f"{label}: {_count_pdfs(folder)} PDFs in {folder}")  # noqa: T201
    return report


def main() -> None:
    """CLI entry point: build every fixture and summarise the counts."""
    print(f"Building Phase 3 fixtures under {FIXTURE_ROOT}")  # noqa: T201
    build_all()


if __name__ == "__main__":
    main()
