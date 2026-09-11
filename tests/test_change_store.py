"""Storing a comparison result in the project file.

Two rules under test: millimetres go in rather than pixels, and everything a
noise filter removed is stored with the reason it was removed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from engine.compare.orchestrator import ComparisonResult
from engine.compare.tolerance import ToleranceSpec, resolve_for_sheet
from engine.compare.types import (
    Bbox,
    ChangeKind,
    ChangeRecord,
    FilteredChange,
    HatchChangeDetail,
    Stream,
    StreamStats,
    TextCategory,
    TextChangeDetail,
    Warning_,
)
from engine.masking.titleblock_zones import RevisionRow
from engine.storage.change_store import (
    change_as_dict,
    clear_comparison,
    comparison_stats,
    load_changes,
    load_filtered,
    load_run,
    run_as_dict,
    save_comparison,
)
from engine.storage.schema import Change, File, Pair, Project, Sheet

TOLERANCE = resolve_for_sheet(ToleranceSpec(dpi=200), scale_text="1:100", dpi=200)


@pytest.fixture
def pair_id(project_db) -> int:
    """A project with one pair, so the foreign keys are real."""
    with Session(project_db) as session:
        project = Project(name="test")
        session.add(project)
        session.flush()
        file = File(
            project_id=project.id, side="new", abs_path="a.pdf", rel_path="a.pdf", filename="a.pdf"
        )
        session.add(file)
        session.flush()
        sheet = Sheet(file_id=file.id, page_index=0, dwg_number="A-101")
        session.add(sheet)
        session.flush()
        pair = Pair(project_id=project.id, new_sheet_id=sheet.id, status="matched")
        session.add(pair)
        session.commit()
        return pair.id


def sample_result() -> ComparisonResult:
    dimension = ChangeRecord(
        kind=ChangeKind.MODIFIED,
        bbox=Bbox(787.4, 393.7, 78.7, 23.6),  # 100, 50, 10, 3 mm at 200 DPI
        streams=[Stream.TEXT, Stream.RASTER],
        description="Dimension changed: 3000 → 3200 (+200 mm, +6.7%)",
        confidence=0.95,
        text=TextChangeDetail(
            category=TextCategory.DIMENSION,
            old_text="3000",
            new_text="3200",
            old_position=(100.0, 200.0),
            new_position=(100.0, 200.0),
            numeric_delta=200.0,
            percent_delta=6.667,
            similarity=0.9,
        ),
    )
    hatch = ChangeRecord(
        kind=ChangeKind.HATCH_PATTERN_CHANGED,
        bbox=Bbox(0, 0, 400, 100),
        streams=[Stream.HATCH],
        description="Hatch pattern changed over 6.3 m²",
        hatch=HatchChangeDetail(
            old_signature="45@2.0mm",
            new_signature="90@2.0mm",
            old_area_m2=6.3,
            new_area_m2=6.3,
            area_delta_m2=0.0,
            segment_count=2000,
        ),
    )
    speckle = ChangeRecord(kind=ChangeKind.ADDED, bbox=Bbox(10, 10, 2, 2), streams=[Stream.RASTER])

    return ComparisonResult(
        pair_id="pair-0",
        changes=[dimension, hatch],
        filtered=[
            FilteredChange(
                change=speckle,
                filter_name="filter_speckle",
                reason="Too small to be a drawn change.",
            )
        ],
        warnings=[Warning_(code="tolerance", message="This sheet has no drawing scale.")],
        stream_stats=[StreamStats(Stream.TEXT, ran=True, change_count=1)],
        tolerance=TOLERANCE,
        revision_rows=[RevisionRow(revision="D", date="01/09/26", description="Doors revised")],
        timings={"text": 0.12},
        config_snapshot={"dpi": 200},
        duration_s=1.8,
    )


def test_a_result_is_stored_in_millimetres_not_pixels(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        change = session.get(Change, 1)
        assert change is not None
        assert change.x == pytest.approx(100.0, abs=0.1)
        assert change.y == pytest.approx(50.0, abs=0.1)
        assert change.w == pytest.approx(10.0, abs=0.1)
        assert change.h == pytest.approx(3.0, abs=0.1)


def test_the_text_behind_a_change_is_stored_with_its_delta(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        change = load_changes(session, pair_id, category="dimension")[0]
        text = change.texts[0]
        assert text.old_text == "3000"
        assert text.new_text == "3200"
        assert text.numeric_delta == pytest.approx(200.0)
        assert text.category == "dimension"
        assert text.kind == "dimension"  # the coarse Phase 1 vocabulary too


def test_a_hatch_region_is_stored_as_one_row(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        change = load_changes(session, pair_id, kind="hatch_pattern_changed")[0]
        assert len(change.hatches) == 1
        assert change.hatches[0].segment_count == 2000
        assert change.hatches[0].old_signature == "45@2.0mm"


def test_which_streams_found_a_change_is_recorded(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        change = load_changes(session, pair_id, stream="text")[0]
        assert change.streams == "text,raster"


def test_everything_filtered_is_stored_with_its_reason(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        filtered = load_filtered(session, pair_id)
        assert len(filtered) == 1
        assert filtered[0].filter_name == "filter_speckle"
        assert "Too small" in filtered[0].reason


def test_the_run_stores_a_reproducible_snapshot(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        run = load_run(session, pair_id)
        assert run is not None
        payload = run_as_dict(run)
        assert payload["config"]["dpi"] == 200
        assert payload["tolerance"]["position"]["site_mm"] == 25.0
        assert payload["warnings"][0]["code"] == "tolerance"
        assert payload["revision_rows"][0]["description"] == "Doors revised"
        assert payload["timings"]["text"] == pytest.approx(0.12)


def test_rerunning_a_comparison_replaces_rather_than_doubles(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()
        save_comparison(session, pair_id, sample_result())
        session.commit()

        assert len(load_changes(session, pair_id, include_cosmetic=True)) == 2
        assert len(load_filtered(session, pair_id)) == 1


def test_a_comparison_can_be_cleared(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()
        removed = clear_comparison(session, pair_id)
        session.commit()

        assert removed == 2
        assert load_changes(session, pair_id, include_cosmetic=True) == []
        assert load_run(session, pair_id) is None


def test_statistics_summarise_the_run(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        stats = comparison_stats(session, pair_id)
        assert stats["total"] == 2
        assert stats["reportable"] == 2
        assert stats["filtered"] == 1
        assert stats["by_kind"]["modified"] == 1
        assert stats["by_stream"]["text"] == 1
        assert stats["by_filter"]["filter_speckle"] == 1


def test_a_change_serialises_for_the_api(project_db, pair_id):
    with Session(project_db) as session:
        save_comparison(session, pair_id, sample_result())
        session.commit()

        payload = change_as_dict(load_changes(session, pair_id, category="dimension")[0])
        assert payload["streams"] == ["text", "raster"]
        assert payload["text"]["numeric_delta"] == pytest.approx(200.0)
        assert payload["bbox_mm"]["w"] == pytest.approx(10.0, abs=0.1)
        assert json.dumps(payload)  # serialisable end to end
