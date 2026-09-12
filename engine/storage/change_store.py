"""Task 5.15 (storage) — writing a comparison result to the project file.

Two rules shape this module.

**Millimetres go in, not pixels.** The engine works in pixels because images
do; the database stores millimetres on the paper of the new sheet. A pixel
box is meaningless without the DPI that produced it, and the DPI is a setting
the user can change between runs — so a project reopened at a different
comparison resolution would show every change in the wrong place.

**Nothing suppressed is lost.** Every record a noise filter removed is written
to `filtered_change` with the filter that removed it and a sentence saying
why. "What did you hide, and why?" is a question the application must always
be able to answer, and it can only answer it from stored rows.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from engine.compare.orchestrator import ComparisonResult
from engine.compare.tolerance import ResolvedTolerance, area_px_to_site_m2
from engine.compare.types import Bbox, ChangeRecord
from engine.storage.schema import (
    Change,
    ChangeHatch,
    ChangeText,
    ComparisonRun,
    FilteredChange,
)


def _mm_box(bbox: Bbox, tolerance: ResolvedTolerance | None) -> tuple[float, float, float, float]:
    """A pixel box as millimetres on the new sheet's paper."""
    if tolerance is None or tolerance.px_per_mm <= 0:
        return (bbox.x, bbox.y, bbox.w, bbox.h)
    return bbox.to_mm(tolerance.px_per_mm)


def _streams(change: ChangeRecord) -> str:
    return ",".join(str(stream) for stream in change.streams)


def save_comparison(
    session: Session,
    pair_id: int,
    result: ComparisonResult,
    *,
    replace: bool = True,
) -> ComparisonRun:
    """Write one pair's comparison: the run, its changes and what was filtered.

    ``replace`` clears the previous result for this pair first, which is what
    re-running a comparison means. Without it a second run doubles every
    change and the register silently becomes wrong.
    """
    if replace:
        clear_comparison(session, pair_id)

    tolerance = result.tolerance
    run = ComparisonRun(
        pair_id=pair_id,
        engine_version=result.engine_version,
        config_json=json.dumps(result.config_snapshot, sort_keys=True, default=str),
        tolerance_json=json.dumps(tolerance.as_dict(), default=str) if tolerance else None,
        mask_set_id=result.mask.template_id if result.mask else None,
        stream_stats_json=json.dumps([stat.as_dict() for stat in result.stream_stats]),
        warnings_json=json.dumps([warning.as_dict() for warning in result.warnings]),
        timings_json=json.dumps({name: round(value, 4) for name, value in result.timings.items()}),
        revision_rows_json=json.dumps([row.as_dict() for row in result.revision_rows]),
        skipped=result.skipped,
        skip_reason=result.skip_reason or None,
        timed_out=result.timed_out,
        duration_s=result.duration_s,
    )
    session.add(run)
    session.flush()

    for change in result.changes:
        session.add(_change_row(pair_id, change, tolerance))

    for entry in result.filtered:
        x, y, width, height = _mm_box(entry.change.bbox, tolerance)
        session.add(
            FilteredChange(
                pair_id=pair_id,
                run_id=run.id,
                x=x,
                y=y,
                w=width,
                h=height,
                kind=str(entry.change.kind),
                streams=_streams(entry.change),
                description=entry.change.description,
                filter_name=entry.filter_name,
                reason=entry.reason,
            )
        )

    session.flush()
    logger.info(
        "Saved comparison | pair={} | changes={} | filtered={}",
        pair_id,
        len(result.changes),
        len(result.filtered),
    )
    return run


def _change_row(pair_id: int, change: ChangeRecord, tolerance: ResolvedTolerance | None) -> Change:
    x, y, width, height = _mm_box(change.bbox, tolerance)
    area_m2 = None
    if tolerance is not None:
        pixel_area = float(change.detail.get("pixel_area", change.bbox.area))
        area_m2 = area_px_to_site_m2(pixel_area, tolerance)

    row = Change(
        pair_id=pair_id,
        x=x,
        y=y,
        w=width,
        h=height,
        type=change.kind.coarse(),
        kind=str(change.kind),
        streams=_streams(change),
        category=change.category,
        confidence=change.confidence,
        is_cosmetic=change.is_cosmetic,
        description=change.description,
        area_m2=area_m2,
        geometry_type=change.vector.geometry_type if change.vector else None,
        detail_json=json.dumps(change.detail, default=str) if change.detail else None,
    )

    if change.text is not None:
        detail = change.text
        row.texts.append(
            ChangeText(
                old_text=detail.old_text,
                new_text=detail.new_text,
                kind=_text_kind(str(detail.category)),
                category=str(detail.category),
                old_x=detail.old_position[0] if detail.old_position else None,
                old_y=detail.old_position[1] if detail.old_position else None,
                new_x=detail.new_position[0] if detail.new_position else None,
                new_y=detail.new_position[1] if detail.new_position else None,
                distance_moved_mm=detail.distance_moved_site_mm,
                numeric_delta=detail.numeric_delta,
                percent_delta=detail.percent_delta,
                cross_check_flag=str(detail.cross_check),
                similarity=detail.similarity,
            )
        )

    if change.hatch is not None:
        detail = change.hatch
        row.hatches.append(
            ChangeHatch(
                old_signature=detail.old_signature,
                new_signature=detail.new_signature,
                old_angle_deg=detail.old_angle_deg,
                new_angle_deg=detail.new_angle_deg,
                old_area_m2=detail.old_area_m2,
                new_area_m2=detail.new_area_m2,
                area_delta_m2=detail.area_delta_m2,
                segment_count=detail.segment_count,
            )
        )
    return row


#: The Phase 1 `TextChangeKind` is coarser than the Phase 5 category, so the
#: finer value is kept beside it rather than replacing it.
_TEXT_KINDS = {
    "dimension": "dimension",
    "level": "dimension",
    "tag": "tag",
    "grid": "tag",
    "note": "note",
    "spec": "note",
    "room": "note",
}


def _text_kind(category: str) -> str | None:
    return _TEXT_KINDS.get(category)


def clear_comparison(session: Session, pair_id: int) -> int:
    """Delete a pair's previous comparison. Returns how many changes went."""
    existing = session.scalars(select(Change).where(Change.pair_id == pair_id)).all()
    count = len(existing)
    for change in existing:
        session.delete(change)
    session.execute(delete(FilteredChange).where(FilteredChange.pair_id == pair_id))
    session.execute(delete(ComparisonRun).where(ComparisonRun.pair_id == pair_id))
    session.flush()
    return count


# ── Reading back ────────────────────────────────────────────────────────


def load_changes(
    session: Session,
    pair_id: int,
    *,
    stream: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    include_cosmetic: bool = False,
    user_status: str | None = None,
) -> list[Change]:
    """A pair's changes, filtered the way the report screen asks for them.

    Cosmetic changes are excluded by default. That is the whole posture of
    the phase: show the real ones, keep the rest one click away.
    """
    statement = select(Change).where(Change.pair_id == pair_id)
    if not include_cosmetic:
        statement = statement.where(Change.is_cosmetic.is_(False))
    if kind:
        statement = statement.where(Change.kind == kind)
    if category:
        statement = statement.where(Change.category == category)
    if user_status:
        statement = statement.where(Change.user_status == user_status)

    rows = list(session.scalars(statement).all())
    if stream:
        rows = [row for row in rows if stream in (row.streams or "")]
    rows.sort(key=lambda row: (-(row.confidence or 0.0), row.id))
    return rows


def load_filtered(session: Session, pair_id: int) -> list[FilteredChange]:
    """What was suppressed, and why. The debug view's whole content."""
    return list(
        session.scalars(select(FilteredChange).where(FilteredChange.pair_id == pair_id)).all()
    )


def load_run(session: Session, pair_id: int) -> ComparisonRun | None:
    return session.scalars(
        select(ComparisonRun)
        .where(ComparisonRun.pair_id == pair_id)
        .order_by(ComparisonRun.id.desc())
    ).first()


def change_as_dict(change: Change) -> dict[str, Any]:
    """One change, shaped for the API."""
    text = change.texts[0] if change.texts else None
    hatch = change.hatches[0] if change.hatches else None
    return {
        "id": change.id,
        "pair_id": change.pair_id,
        "bbox_mm": {"x": change.x, "y": change.y, "w": change.w, "h": change.h},
        "type": change.type,
        "kind": change.kind,
        "streams": (change.streams or "").split(",") if change.streams else [],
        "category": change.category,
        "confidence": change.confidence,
        "is_cosmetic": change.is_cosmetic,
        "is_clouded": change.is_clouded,
        "description": change.description,
        "area_m2": change.area_m2,
        "geometry_type": change.geometry_type,
        "user_status": change.user_status,
        "text": None
        if text is None
        else {
            "category": text.category,
            "old_text": text.old_text,
            "new_text": text.new_text,
            "distance_moved_mm": text.distance_moved_mm,
            "numeric_delta": text.numeric_delta,
            "percent_delta": text.percent_delta,
            "cross_check_flag": text.cross_check_flag,
            "similarity": text.similarity,
        },
        "hatch": None
        if hatch is None
        else {
            "old_signature": hatch.old_signature,
            "new_signature": hatch.new_signature,
            "old_area_m2": hatch.old_area_m2,
            "new_area_m2": hatch.new_area_m2,
            "area_delta_m2": hatch.area_delta_m2,
            "segment_count": hatch.segment_count,
        },
    }


def run_as_dict(run: ComparisonRun) -> dict[str, Any]:
    """The run record, shaped for the API."""

    def parse(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback

    return {
        "id": run.id,
        "pair_id": run.pair_id,
        "engine_version": run.engine_version,
        "config": parse(run.config_json, {}),
        "tolerance": parse(run.tolerance_json, None),
        "mask_set_id": run.mask_set_id,
        "streams": parse(run.stream_stats_json, []),
        "warnings": parse(run.warnings_json, []),
        "timings": parse(run.timings_json, {}),
        "revision_rows": parse(run.revision_rows_json, []),
        "skipped": run.skipped,
        "skip_reason": run.skip_reason,
        "timed_out": run.timed_out,
        "duration_s": run.duration_s,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def comparison_stats(session: Session, pair_id: int) -> dict[str, Any]:
    """Counts by stream, kind and category, plus what was filtered."""
    changes = list(session.scalars(select(Change).where(Change.pair_id == pair_id)).all())
    filtered = load_filtered(session, pair_id)

    by_kind: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_stream: dict[str, int] = {}
    for change in changes:
        by_kind[change.kind or "unknown"] = by_kind.get(change.kind or "unknown", 0) + 1
        key = change.category or "unknown"
        by_category[key] = by_category.get(key, 0) + 1
        for stream in (change.streams or "").split(","):
            if stream:
                by_stream[stream] = by_stream.get(stream, 0) + 1

    by_filter: dict[str, int] = {}
    for entry in filtered:
        by_filter[entry.filter_name] = by_filter.get(entry.filter_name, 0) + 1

    return {
        "pair_id": pair_id,
        "total": len(changes),
        "reportable": sum(1 for change in changes if not change.is_cosmetic),
        "cosmetic": sum(1 for change in changes if change.is_cosmetic),
        "filtered": len(filtered),
        "by_kind": by_kind,
        "by_category": by_category,
        "by_stream": by_stream,
        "by_filter": by_filter,
    }
