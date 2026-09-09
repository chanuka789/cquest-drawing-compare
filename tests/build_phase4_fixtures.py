"""Generate the Phase 4 alignment fixture folders, which live outside the repo.

Run as a module so pytest collection never triggers the build:

    python -m tests.build_phase4_fixtures

The fixtures are written under :data:`FIXTURE_ROOT` (default
``D:\\GitHub\\_cqdc-fixtures`` per CLAUDE.md, overridable with the
``CQDC_FIXTURE_ROOT`` environment variable), under ``10_alignment\\`` with one
folder per case, each holding an ``old`` and a ``new`` issue:

* ``clean_pair``  — identical sheets except one changed label, Rev C -> Rev D
* ``shifted``     — the same content plotted 40 mm to the right
* ``rescaled``    — A-101 at 1:100 re-issued with its content doubled, 1 : 50
* ``rotated``     — the content rotated 90 degrees about the page centre
* ``page_rotated`` — byte-similar content with a different ``/Rotate`` flag
* ``scanned``     — the old sheet printed out and scanned back in (skewed)
* ``no_grid``     — a detail sheet with no grid, hatch or circles
* ``sparse_text`` — a geometry-heavy sheet with almost no text
* ``impossible``  — two genuinely different drawings, which MUST be refused

All of the heavy work happens only when ``main()`` runs; importing this module
never writes a file.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.fixture_builder import ALIGNMENT_CASES

#: Where the golden fixtures live: outside the repository, per CLAUDE.md.
FIXTURE_ROOT = Path(os.environ.get("CQDC_FIXTURE_ROOT", r"D:\GitHub\_cqdc-fixtures"))


def _count_pdfs(folder: Path) -> int:
    return len(list(folder.glob("*.pdf")))


def build_all(root: Path | None = None) -> dict[str, Path]:
    """Create every Phase 4 fixture under *root* and report what was produced.

    Returns a map of ``10_alignment/<case>`` to its folder. Re-runs overwrite
    the drawings in place; nothing is deleted.
    """
    base = Path(root) if root is not None else FIXTURE_ROOT
    base.mkdir(parents=True, exist_ok=True)

    report: dict[str, Path] = {}
    for name, builder in ALIGNMENT_CASES:
        case_dir = base / "10_alignment" / name
        builder(case_dir)
        report[f"10_alignment/{name}"] = case_dir

    for label, folder in report.items():
        old_count = _count_pdfs(folder / "old")
        new_count = _count_pdfs(folder / "new")
        print(f"{label}: {old_count} old + {new_count} new PDFs in {folder}")  # noqa: T201
    return report


def main() -> None:
    """CLI entry point: build every fixture and summarise the counts."""
    print(f"Building Phase 4 fixtures under {FIXTURE_ROOT}")  # noqa: T201
    build_all()


if __name__ == "__main__":
    main()
