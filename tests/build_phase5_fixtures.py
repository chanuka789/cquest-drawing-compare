"""Generate the Phase 5 comparison fixtures, which live outside the repo.

Run as a module so pytest collection never triggers the build:

    python -m tests.build_phase5_fixtures
    python -m tests.build_phase5_fixtures --golden   # also refresh the golden file

The fixtures are written under :data:`FIXTURE_ROOT` (default
``D:\\GitHub\\_cqdc-fixtures`` per CLAUDE.md, overridable with the
``CQDC_FIXTURE_ROOT`` environment variable) into ``11_comparison\\``, one
folder per case holding ``old`` and ``new`` issues plus a ``truth.json``
recording exactly which changes were injected.

Half the cases are cosmetic-only and must report **zero** changes.
``rev_letter_only`` is the most important fixture in the project: two
identical sheets whose revision letter differs. Every noise filter is
measured against it.

Importing this module never writes a file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from tests.harness.inject import InjectedPair, inject_changes
from tests.phase5_cases import Case, all_cases, write_source

#: Where the golden fixtures live: outside the repository, per CLAUDE.md.
FIXTURE_ROOT = Path(os.environ.get("CQDC_FIXTURE_ROOT", r"D:\GitHub\_cqdc-fixtures"))
#: The regression file that lives *inside* the repo, beside the tests.
GOLDEN_PATH = Path(__file__).with_name("phase5_golden.json")
#: Comparison resolution used when refreshing the golden file.
GOLDEN_DPI = 150


def build_case(case: Case, root: Path, source: Path) -> InjectedPair:
    """Write one case into ``11_comparison/<name>/{old,new}`` with its truth."""
    case_dir = root / "11_comparison" / case.name
    staging = case_dir / "_build"
    pair = inject_changes(source, 0, case.spec, out_dir=staging)

    old_dir = case_dir / "old"
    new_dir = case_dir / "new"
    old_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)
    old_target = old_dir / f"{case.name}.pdf"
    new_target = new_dir / f"{case.name}.pdf"
    shutil.copyfile(pair.old_path, old_target)
    shutil.copyfile(pair.new_path, new_target)
    shutil.rmtree(staging, ignore_errors=True)

    truth = pair.as_dict()
    truth["old"] = str(old_target)
    truth["new"] = str(new_target)
    truth["purpose"] = case.purpose
    (case_dir / "truth.json").write_text(
        json.dumps(truth, indent=2, sort_keys=True), encoding="utf-8"
    )

    pair.old_path = old_target
    pair.new_path = new_target
    return pair


def build_all(root: Path | None = None) -> dict[str, InjectedPair]:
    """Create every Phase 5 fixture under *root*."""
    base = Path(root) if root is not None else FIXTURE_ROOT
    base.mkdir(parents=True, exist_ok=True)
    source = write_source(base / "11_comparison" / "_source" / "A-101.pdf")

    built: dict[str, InjectedPair] = {}
    for case in all_cases():
        built[case.name] = build_case(case, base, source)
    return built


def refresh_golden(root: Path | None = None) -> Path:
    """Re-run the matrix and write the regression file.

    Deliberate, never reflexive: the golden file is what stops a change that
    quietly lowers precision from passing the suite.
    """
    from engine.compare.orchestrator import CompareConfig
    from engine.compare.tolerance import ToleranceSpec
    from tests.harness.precision_recall import MatrixReport, run_case

    base = Path(root) if root is not None else FIXTURE_ROOT
    work = base / "11_comparison" / "_matrix"
    work.mkdir(parents=True, exist_ok=True)
    source = write_source(work / "source.pdf")

    config = CompareConfig(dpi=GOLDEN_DPI, tolerance=ToleranceSpec(dpi=GOLDEN_DPI))
    report = MatrixReport()
    for case in all_cases():
        _pair, _result, metrics = run_case(source, case.spec, work, config=config)
        report.pairs.append(metrics)

    report.write_csv(work / "matrix.csv")
    report.write_golden(GOLDEN_PATH)
    print(report.table())  # noqa: T201
    return GOLDEN_PATH


def main() -> None:
    """CLI entry point: build every fixture, optionally refresh the golden."""
    print(f"Building Phase 5 fixtures under {FIXTURE_ROOT}")  # noqa: T201
    built = build_all()
    for name, pair in sorted(built.items()):
        kind = "cosmetic-only" if pair.is_cosmetic_only else f"{len(pair.expected)} change(s)"
        print(f"  11_comparison/{name}: {kind}")  # noqa: T201

    if "--golden" in sys.argv:
        print("\nRefreshing the golden regression file...")  # noqa: T201
        path = refresh_golden()
        print(f"\nWrote {path}")  # noqa: T201


if __name__ == "__main__":
    main()
