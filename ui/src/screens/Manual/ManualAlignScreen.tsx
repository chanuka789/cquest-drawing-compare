/**
 * Screen 6 — Manual alignment (Phase 4, Task 4.13).
 *
 * The escape hatch the review screen offers when the cascade refused a
 * pair: two independent, pannable/zoomable sheet panes, and the user clicks
 * matching points — a grid intersection, a column centre, a building corner.
 * Each old-side click produces a numbered pending marker; the next click on
 * the new sheet turns it into pair N. Two pairs are the minimum.
 *
 * From two pairs on, a similarity fit is computed client-side from the first
 * two pairs (rotation = angle difference, scale = length ratio, translation
 * through the centroids) and shown as a live overlay preview, with each
 * point's residual printed in millimetres on paper (200 dpi: one pixel is
 * 25.4/200 mm). With exactly two pairs the residual is 0 by construction;
 * a third pair makes a badly placed point show up before anything is sent.
 *
 * "Apply alignment" posts the points to the engine, which fits its own
 * transform and runs the same quality gate as the automatic path; the
 * returned row replaces the batch row this screen was opened from, and the
 * screen returns to Alignment with that detail open.
 */

import { useEffect, useMemo, useState } from 'react';

import { Lightbox } from '../../components/Lightbox';
import { ApiError, postManualAlignment } from '../../api/client';
import type { AlignResultRow, ManualPoint } from '../../api/types';
import { truncateMiddle } from '../../lib/path';
import { useAppStore } from '../../store/appStore';
import { manualTarget, useAlignStore } from '../../store/alignStore';
import { ManualSheetCanvas } from './ManualSheetCanvas';
import type { SheetMarker } from './ManualSheetCanvas';

import './ManualAlignScreen.css';

/** The sheets both panes show were rendered at 200 dpi, so px/mm = 200/25.4. */
const PX_PER_MM = 200 / 25.4;

/** One point the user clicked, stored raw in SHEET pixels at 200 dpi. */
interface PlacedPoint {
  x: number;
  y: number;
}

/** One completed correspondence: point N on the old sheet and on the new. */
interface PlacedPair {
  old: PlacedPoint;
  new: PlacedPoint;
}

function message(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Something went wrong. The details are in the log file.';
}

export function ManualAlignScreen() {
  const goToAlignment = useAppStore((store) => store.goToAlignment);
  // The row this screen acts on was fixed when the user pressed "Align
  // manually" in the Alignment detail; snapshot it once on mount.
  const target = useMemo(() => manualTarget(useAlignStore.getState()), []);

  const [pairs, setPairs] = useState<PlacedPair[]>([]);
  const [pendingOld, setPendingOld] = useState<PlacedPoint | null>(null);
  const [sideHint, setSideHint] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  // Without a target row this screen is meaningless: drop back to Alignment.
  useEffect(() => {
    if (!target) goToAlignment();
  }, [target, goToAlignment]);

  // Keep the store's selection on this row so returning shows its detail and
  // `applyManualRow` (which replaces the selected row) lands in the right
  // place even if the store was left mid-way between screens.
  useEffect(() => {
    if (target) useAlignStore.getState().selectRow(target.index);
  }, [target]);

  const oldSheetId = target?.row.old.sheet_id ?? null;
  const newSheetId = target?.row.new.sheet_id ?? null;
  const sheetsAvailable = oldSheetId !== null && newSheetId !== null;

  // ── Markers per pane, numbered in pair order ─────────────────────────
  const oldMarkers: SheetMarker[] = pairs.map((pair, index) => ({
    x: pair.old.x,
    y: pair.old.y,
    number: index + 1,
    pending: false,
  }));
  if (pendingOld) {
    oldMarkers.push({
      x: pendingOld.x,
      y: pendingOld.y,
      number: pairs.length + 1,
      pending: true,
    });
  }
  const newMarkers: SheetMarker[] = pairs.map((pair, index) => ({
    x: pair.new.x,
    y: pair.new.y,
    number: index + 1,
    pending: false,
  }));

  const pickOld = (x: number, y: number): void => {
    setSideHint(null);
    setApplyError(null);
    // Placing again while one is pending simply moves that marker.
    setPendingOld({ x, y });
  };

  const pickNew = (x: number, y: number): void => {
    setApplyError(null);
    if (pendingOld === null) {
      setSideHint('Mark a point on the previous sheet first — its marker turns into a pair.');
      return;
    }
    setSideHint(null);
    setPairs((current) => [...current, { old: pendingOld, new: { x, y } }]);
    setPendingOld(null);
  };

  const undoLast = (): void => {
    setApplyError(null);
    if (pendingOld !== null) {
      setPendingOld(null);
      return;
    }
    setPairs((current) => current.slice(0, -1));
  };

  const clearAll = (): void => {
    setPairs([]);
    setPendingOld(null);
    setApplyError(null);
    setSideHint(null);
  };

  // ── Live similarity preview from the first two pairs ─────────────────
  const fit = useMemo(() => similarityFromFirstTwo(pairs), [pairs]);
  const canPreview = sheetsAvailable && fit !== null;

  const apply = async (): Promise<void> => {
    if (target === null || oldSheetId === null || newSheetId === null) return;
    setSubmitting(true);
    setApplyError(null);
    try {
      const points: ManualPoint[] = pairs.map((pair) => ({
        old_x: pair.old.x,
        old_y: pair.old.y,
        new_x: pair.new.x,
        new_y: pair.new.y,
      }));
      const row: AlignResultRow = await postManualAlignment({
        old_sheet_id: oldSheetId,
        new_sheet_id: newSheetId,
        points,
        index: target.index,
      });
      const store = useAlignStore.getState();
      store.applyManualRow(row);
      goToAlignment();
    } catch (error) {
      setApplyError(message(error));
      setSubmitting(false);
    }
  };

  const hasAny = pairs.length > 0 || pendingOld !== null;
  const canApply = sheetsAvailable && pairs.length >= 2 && !submitting;
  const headerLine =
    target === null
      ? 'No pair selected.'
      : `${truncateMiddle(target.row.old.filename, 52)} → ${truncateMiddle(
          target.row.new.filename,
          52,
        )}`;

  if (target === null) {
    return <div className="manual" />;
  }

  return (
    <div className="manual">
      <header className="manual__head">
        <div className="manual__title-row">
          <button type="button" className="manual__back" onClick={goToAlignment}>
            ← Alignment
          </button>
          <h1>Manual alignment</h1>
        </div>
        <p className="manual__sentence" title={headerLine}>
          {headerLine}
        </p>
      </header>

      {!sheetsAvailable && (
        <p className="manual__error" role="alert">
          One of the two sheets has no rendered tiles, so this pair cannot be
          aligned by hand. Re-run the alignment and try again.
        </p>
      )}
      {applyError && (
        <p className="manual__error" role="alert">
          {applyError}
        </p>
      )}

      <p className="manual__hint micro">
        Pick matching points: grid intersections, column centres, building
        corners — two points minimum.
      </p>

      <div className="manual__canvases">
        <ManualSheetCanvas
          sheetId={oldSheetId}
          filename={target.row.old.filename}
          markers={oldMarkers}
          canPick={sheetsAvailable}
          onPick={pickOld}
        />
        <ManualSheetCanvas
          sheetId={newSheetId}
          filename={target.row.new.filename}
          markers={newMarkers}
          canPick={sheetsAvailable}
          onPick={pickNew}
        />
      </div>

      <div className="manual__toolbar">
        <p className="manual__status micro" role="status">
          {pendingOld !== null ? (
            <>
              Point <span className="tabular">{pairs.length + 1}</span> marked on
              the previous sheet — now click its match on the current sheet.
            </>
          ) : pairs.length === 0 ? (
            'No points placed yet.'
          ) : (
            <>
              <span className="tabular">{pairs.length}</span>{' '}
              {pairs.length === 1 ? 'pair placed' : 'pairs placed'} —{' '}
              {pairs.length < 2
                ? 'one more pair unlocks the preview and Apply.'
                : 'the preview below follows the current fit.'}
            </>
          )}
          {sideHint !== null && <span className="manual__side-hint">{sideHint}</span>}
        </p>
        <div className="manual__toolbar-actions">
          <button
            type="button"
            className="manual__quiet"
            onClick={undoLast}
            disabled={!hasAny || submitting}
          >
            Undo last point
          </button>
          <button
            type="button"
            className="manual__quiet"
            onClick={clearAll}
            disabled={!hasAny || submitting}
          >
            Clear all
          </button>
          <button
            type="button"
            className="button button--primary manual__apply"
            onClick={() => void apply()}
            disabled={!canApply}
            title={
              sheetsAvailable && pairs.length < 2
                ? 'Place at least two pairs of points first'
                : undefined
            }
          >
            {submitting ? 'Applying…' : 'Apply alignment'}
          </button>
        </div>
      </div>

      {/* ── The live preview ── */}
      <section className="manual__preview" aria-label="Live alignment preview">
        <header className="manual__preview-head">
          <h2 className="manual__preview-title">Live preview</h2>
          <p className="manual__preview-note micro">
            {fit === null
              ? 'A similarity fit needs at least two pairs spread apart.'
              : `${pairs.length} pairs · scale ${fit.scale.toFixed(3)}× · rotation ${fit.rotationDeg.toFixed(1)}° · shift ${(fit.shiftPx / PX_PER_MM).toFixed(1)} mm on paper`}
          </p>
        </header>
        <div className="manual__preview-body">
          <div className="manual__preview-lightbox">
            {/* mode must be explicit: without it the Lightbox restores the
                user's remembered mode; this preview always wants the overlay.
                controls=false keeps the control bar off this screen. */}
            <Lightbox
              oldSheetId={oldSheetId}
              newSheetId={newSheetId}
              transformMatrix={canPreview ? fit.matrix : null}
              dpi={200}
              mode="overlay"
              controls={false}
            />
          </div>
          <div className="manual__residuals">
            <p className="manual__residuals-title micro">Point error on paper</p>
            {pairs.length < 2 ? (
              <p className="manual__residuals-empty micro">
                {pendingOld !== null
                  ? 'Waiting for its match on the current sheet…'
                  : 'Place two pairs of points to see the estimated error.'}
              </p>
            ) : fit === null ? (
              <p className="manual__residuals-empty micro">
                The first two points sit on top of each other — move them apart
                to fit a similarity transform.
              </p>
            ) : (
              <ul className="manual__residuals-list">
                {pairs.map((pair, index) => (
                  <ResidualRow
                    key={index}
                    number={index + 1}
                    pair={pair}
                    matrix={fit.matrix}
                  />
                ))}
              </ul>
            )}
            {pairs.length === 2 && (
              <p className="manual__residuals-note micro">
                Two points fit exactly by construction — the error shows up
                when a third pair disagrees.
              </p>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

/** One point's residual under the preview transform, in mm on paper. */
function ResidualRow({
  number,
  pair,
  matrix,
}: {
  number: number;
  pair: PlacedPair;
  matrix: number[];
}) {
  const mapped = applySimilarity(matrix, pair.old.x, pair.old.y);
  const residualPx = Math.hypot(mapped.x - pair.new.x, mapped.y - pair.new.y);
  return (
    <li className="manual__residual">
      <span className="manual__residual-name tabular">Point {number}</span>
      <span className="manual__residual-value tabular">
        {(residualPx / PX_PER_MM).toFixed(2)} mm
      </span>
    </li>
  );
}

// ── Pure similarity maths (client preview only; the engine refits) ─────

/**
 * A similarity fit from the first two pairs: rotation is the angle between
 * the new and old pair vectors, scale their length ratio, translation so the
 * centroid of the old pair lands on the centroid of the new pair. Returns
 * the row-major 3×3 matrix as the Lightbox expects it (flat, 9 elements) or
 * null when the pairs are degenerate.
 */
function similarityFromFirstTwo(
  pairs: PlacedPair[],
): { matrix: number[]; scale: number; rotationDeg: number; shiftPx: number } | null {
  if (pairs.length < 2) return null;
  const a = pairs[0];
  const b = pairs[1];
  if (!a || !b) return null;

  const oldDx = b.old.x - a.old.x;
  const oldDy = b.old.y - a.old.y;
  const newDx = b.new.x - a.new.x;
  const newDy = b.new.y - a.new.y;
  const oldLength = Math.hypot(oldDx, oldDy);
  const newLength = Math.hypot(newDx, newDy);
  if (!(oldLength > 1e-6) || !(newLength > 1e-6)) return null;

  const scale = newLength / oldLength;
  const rotation = Math.atan2(newDy, newDx) - Math.atan2(oldDy, oldDx);
  const cosT = Math.cos(rotation);
  const sinT = Math.sin(rotation);
  const a11 = scale * cosT;
  const a12 = -scale * sinT;
  const a21 = scale * sinT;
  const a22 = scale * cosT;

  const oldCx = (a.old.x + b.old.x) / 2;
  const oldCy = (a.old.y + b.old.y) / 2;
  const newCx = (a.new.x + b.new.x) / 2;
  const newCy = (a.new.y + b.new.y) / 2;
  const tx = newCx - (a11 * oldCx + a12 * oldCy);
  const ty = newCy - (a21 * oldCx + a22 * oldCy);

  const matrix = [a11, a12, tx, a21, a22, ty, 0, 0, 1];
  if (!matrix.every(Number.isFinite)) return null;
  return { matrix, scale, rotationDeg: (rotation * 180) / Math.PI, shiftPx: Math.hypot(tx, ty) };
}

/** Apply the row-major matrix (old → new) to one sheet point. */
function applySimilarity(matrix: number[], x: number, y: number): { x: number; y: number } {
  if (matrix.length !== 9) return { x, y };
  const [m00, m01, m02, m10, m11, m12] = matrix as [
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  return {
    x: m00 * x + m01 * y + m02,
    y: m10 * x + m11 * y + m12,
  };
}
