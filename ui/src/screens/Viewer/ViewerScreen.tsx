/**
 * The full-size drawing viewer.
 *
 * Phase 4 built a real lightbox — four view modes, pan and zoom, a minimap,
 * keyboard navigation — and then only ever mounted it inside a 200-pixel
 * detail panel with its controls switched off, which is the same as not
 * having built it. This screen is its home: the sheet gets the whole
 * window, and the change list rides along beside it.
 *
 * Tab and Shift+Tab step through the changes, which is how a reviewer
 * actually works: look at one, decide, move on.
 */

import { useCallback, useEffect } from 'react';

import { Lightbox } from '../../components/Lightbox';
import { StatusPill } from '../../components/StatusPill';
import { useAlignStore } from '../../store/alignStore';
import { useAppStore } from '../../store/appStore';
import {
  SEVERITY_LABEL,
  changesForSheet,
  severityTone,
  useCompareStore,
} from '../../store/compareStore';

import './ViewerScreen.css';

/** The alignment matrix for a pair, flattened for the lightbox. */
function flattenMatrix(matrix: number[][] | null): number[] | null {
  if (matrix === null || matrix.length !== 3) return null;
  const flat: number[] = [];
  for (const row of matrix) {
    if (!row || row.length !== 3) return null;
    flat.push(...row);
  }
  return flat;
}

export function ViewerScreen() {
  const pairIndex = useAppStore((store) => store.viewerPair);
  const go = useAppStore((store) => store.go);
  const compare = useCompareStore();
  const selectRegion = useCompareStore((store) => store.selectRegion);
  const alignResults = useAlignStore((store) => store.results);

  const sheet = pairIndex === null ? undefined : compare.results[pairIndex];
  const regions = pairIndex === null ? [] : changesForSheet(compare, pairIndex);
  const selected = compare.selectedRegion;

  // The alignment row sits at the same index as the compare row only when
  // every pair aligned. Match on the filename instead, which is stable.
  const alignRow = alignResults.find(
    (row) => sheet && row.new.sheet_id === sheet.new.sheet_id,
  );

  const step = useCallback(
    (delta: number) => {
      if (regions.length === 0) return;
      const current = regions.findIndex((region) => region.index === selected);
      const next = (current + delta + regions.length) % regions.length;
      const target = regions[next];
      if (target) selectRegion(target.index);
    },
    [regions, selected, selectRegion],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (event.key === 'Tab') {
        event.preventDefault();
        step(event.shiftKey ? -1 : 1);
      } else if (event.key === 'Escape') {
        go('changes');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [step, go]);

  if (pairIndex === null || !sheet) {
    return (
      <main className="viewer viewer--empty">
        <p className="viewer__hint">No sheet is open.</p>
        <button type="button" className="button" onClick={() => go('changes')}>
          Back to the changes
        </button>
      </main>
    );
  }

  const region = regions.find((item) => item.index === selected) ?? null;

  return (
    <main className="viewer">
      <header className="viewer__head">
        <button type="button" className="viewer__back" onClick={() => go('changes')}>
          ← Changes
        </button>
        <div className="viewer__pair">
          <span className="viewer__file" title={sheet.old.filename}>
            {sheet.old.filename}
          </span>
          <span className="viewer__arrow" aria-hidden="true">
            →
          </span>
          <span className="viewer__file" title={sheet.new.filename}>
            {sheet.new.filename}
          </span>
        </div>
        <p className="viewer__count micro" role="status">
          {regions.length === 0
            ? 'No changes on this sheet'
            : `${regions.length} ${regions.length === 1 ? 'change' : 'changes'} · Tab to step through`}
        </p>
      </header>

      <div className="viewer__body">
        <div className="viewer__canvas">
          <Lightbox
            oldSheetId={sheet.old.sheet_id}
            newSheetId={sheet.new.sheet_id}
            transformMatrix={alignRow ? flattenMatrix(alignRow.matrix) : null}
            dpi={sheet.dpi}
            controls
            changeRegions={regions}
            selectedRegion={selected}
            onSelectRegion={selectRegion}
          />
        </div>

        <aside className="viewer__side" aria-label="Changes on this sheet">
          {regions.length === 0 ? (
            <p className="viewer__hint">
              Nothing changed on this sheet, or the changes are cosmetic and
              hidden.
            </p>
          ) : (
            <ol className="viewer__list">
              {regions.map((item) => (
                <li key={item.index}>
                  <button
                    type="button"
                    className="viewer__item"
                    aria-current={item.index === selected ? 'true' : undefined}
                    onClick={() => selectRegion(item.index)}
                  >
                    <StatusPill tone={severityTone(item.severity)}>
                      {SEVERITY_LABEL[item.severity]}
                    </StatusPill>
                    <span className="viewer__item-what">{item.explanation}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}

          {region && (
            <footer className="viewer__detail">
              <p className="viewer__detail-what">{region.explanation}</p>
              {region.old_text !== null && region.new_text !== null && (
                <p className="viewer__detail-text tabular">
                  <span className="viewer__was">{region.old_text || '—'}</span>
                  <span aria-hidden="true"> → </span>
                  <span className="viewer__now">{region.new_text || '—'}</span>
                </p>
              )}
            </footer>
          )}
        </aside>
      </div>
    </main>
  );
}
