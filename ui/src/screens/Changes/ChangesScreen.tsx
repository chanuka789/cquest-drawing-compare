/**
 * The Changes screen (Phase 5): what actually differs, sheet by sheet.
 *
 * The list is the product. A quantity surveyor walks it top to bottom, and
 * every row has to be readable without knowing anything about how the app
 * works — so each row leads with the change in words ("A dimension changed
 * from 3200 to 3400"), and the measurements are always millimetres, on
 * paper and at drawing scale. Pixels never reach this screen.
 *
 * Cosmetic changes are hidden by default. The plan's answer to false
 * positives is to keep them out of the way rather than to drop them, so
 * they are one toggle away and never silently discarded.
 */

import { useEffect } from 'react';

import { Lightbox } from '../../components/Lightbox';
import { StatusPill } from '../../components/StatusPill';
import { useAppStore } from '../../store/appStore';
import {
  CHANGE_TYPE_LABEL,
  SEVERITY_LABEL,
  allChanges,
  changesForSheet,
  headline,
  severityCounts,
  severityTone,
  triageKey,
  useCompareStore,
  type ChangeFilter,
} from '../../store/compareStore';
import type { ChangeRegion, ChangeSeverity, CompareResultRow } from '../../api/types';

import './ChangesScreen.css';

const FILTERS: ChangeFilter[] = ['all', 'critical', 'major', 'minor', 'trivial'];

/** Millimetres, or an em dash when the scale could not be read. */
function fmtMm(value: number | null, digits = 1): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value.toFixed(digits)} mm`;
}

/** Square metres at drawing scale — the unit a QS actually thinks in. */
function fmtSiteArea(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${(value / 1_000_000).toFixed(2)} m²`;
}

export function ChangesScreen() {
  const state = useCompareStore();
  const enter = useCompareStore((store) => store.enter);
  const leave = useCompareStore((store) => store.leave);

  useEffect(() => {
    void enter();
    return () => leave();
  }, [enter, leave]);

  if (state.phase === 'idle' && state.results.length === 0) {
    return <EmptyView />;
  }
  if (state.phase === 'running' || state.phase === 'aligning') {
    return <RunningView />;
  }
  if (state.phase === 'failed') {
    return <FailedView />;
  }

  return (
    <main className="changes">
      <ChangesHeader />
      <div className="changes__body">
        <ChangeList />
        <SheetPanel />
      </div>
    </main>
  );
}

// ── Header ─────────────────────────────────────────────────────────────

function ChangesHeader() {
  const state = useCompareStore();
  const setFilter = useCompareStore((store) => store.setFilter);
  const toggleCosmetic = useCompareStore((store) => store.toggleCosmetic);
  const start = useCompareStore((store) => store.start);
  const counts = severityCounts(state);

  return (
    <header className="changes__head">
      <div className="changes__head-top">
        <h1 className="changes__title">Changes</h1>
        <div className="changes__head-actions">
          <button
            type="button"
            className="changes__cosmetic"
            aria-pressed={state.showCosmetic}
            onClick={toggleCosmetic}
          >
            {state.showCosmetic ? 'Hide cosmetic changes' : 'Show cosmetic changes'}
          </button>
          <button type="button" className="button" onClick={() => void start()}>
            Compare again
          </button>
        </div>
      </div>

      <p className="changes__headline" role="status">
        {headline(state)}
      </p>

      <div className="changes__filters">
        {FILTERS.map((filter) => {
          const count = filter === 'all' ? null : counts[filter as ChangeSeverity];
          if (filter !== 'all' && count === 0) return null;
          return (
            <button
              key={filter}
              type="button"
              className="changes__filter"
              aria-pressed={state.filter === filter}
              onClick={() => setFilter(filter)}
            >
              {filter === 'all' ? 'All' : SEVERITY_LABEL[filter as ChangeSeverity]}
              {count !== null && <span className="changes__filter-count tabular"> {count}</span>}
            </button>
          );
        })}
      </div>
    </header>
  );
}

// ── The list ───────────────────────────────────────────────────────────

function ChangeList() {
  const state = useCompareStore();
  const selectSheet = useCompareStore((store) => store.selectSheet);
  const selectRegion = useCompareStore((store) => store.selectRegion);
  const confirm = useCompareStore((store) => store.confirm);
  const dismiss = useCompareStore((store) => store.dismiss);
  const entries = allChanges(state);

  if (entries.length === 0) {
    return (
      <div className="changes__list changes__list--empty">
        <p className="changes__none">
          {state.results.length === 0
            ? 'Nothing has been compared yet.'
            : state.showCosmetic
              ? 'No changes match this filter.'
              : 'No changes match this filter. Cosmetic changes are hidden — show them to check.'}
        </p>
      </div>
    );
  }

  return (
    <div className="changes__list" role="list">
      {entries.map((entry) => {
        const key = triageKey(entry.sheetIndex, entry.region.index);
        const isConfirmed = state.confirmed.has(key);
        const isDismissed = state.dismissed.has(key);
        const isSelected =
          state.selectedSheet === entry.sheetIndex &&
          state.selectedRegion === entry.region.index;

        return (
          <div
            key={key}
            role="listitem"
            className={[
              'changes__row',
              isSelected ? 'changes__row--selected' : '',
              isDismissed ? 'changes__row--dismissed' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          >
            <button
              type="button"
              className="changes__row-main"
              onClick={() => {
                selectSheet(entry.sheetIndex);
                selectRegion(entry.region.index);
              }}
            >
              <span className="changes__row-head">
                <StatusPill tone={severityTone(entry.region.severity)}>
                  {SEVERITY_LABEL[entry.region.severity]}
                </StatusPill>
                <span className="changes__row-type micro">
                  {CHANGE_TYPE_LABEL[entry.region.type] ?? entry.region.type}
                </span>
                <span className="changes__row-sheet micro" title={entry.sheet.new.filename}>
                  {entry.sheet.new.filename}
                </span>
              </span>

              <span className="changes__row-what">{entry.region.explanation}</span>

              <span className="changes__row-facts micro tabular">
                <span>{fmtMm(entry.region.area_mm2, 1)}² on paper</span>
                {entry.region.area_site_mm2 !== null && (
                  <span>{fmtSiteArea(entry.region.area_site_mm2)} at scale</span>
                )}
                {entry.region.is_cosmetic && <span>Cosmetic</span>}
              </span>
            </button>

            <div className="changes__row-triage">
              <button
                type="button"
                className="changes__triage"
                aria-pressed={isConfirmed}
                title="Confirm this change (a note for this session)"
                onClick={() => confirm(entry.sheetIndex, entry.region.index)}
              >
                {isConfirmed ? '✓ Confirmed' : 'Confirm'}
              </button>
              <button
                type="button"
                className="changes__triage"
                aria-pressed={isDismissed}
                title="Dismiss this change (a note for this session)"
                onClick={() => dismiss(entry.sheetIndex, entry.region.index)}
              >
                {isDismissed ? 'Dismissed' : 'Dismiss'}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── The sheet, with the changes drawn on it ────────────────────────────

function SheetPanel() {
  const state = useCompareStore();
  const openViewer = useAppStore((store) => store.openViewer);
  const selectRegion = useCompareStore((store) => store.selectRegion);
  const sheetIndex = state.selectedSheet;
  const sheet: CompareResultRow | undefined =
    sheetIndex === null ? undefined : state.results[sheetIndex];

  if (!sheet || sheetIndex === null) {
    return (
      <aside className="changes__panel changes__panel--empty">
        <p className="changes__hint">Choose a change to see it on the sheet.</p>
      </aside>
    );
  }

  const regions = changesForSheet(state, sheetIndex);
  const selected = regions.find((region) => region.index === state.selectedRegion) ?? null;

  return (
    <aside className="changes__panel" aria-label={`Changes on ${sheet.new.filename}`}>
      <header className="changes__panel-head">
        <span className="changes__panel-file" title={sheet.new.filename}>
          {sheet.new.filename}
        </span>
        <button
          type="button"
          className="changes__open"
          onClick={() => openViewer(sheetIndex)}
        >
          Open in viewer
        </button>
      </header>

      <div className="changes__preview">
        <Lightbox
          oldSheetId={sheet.old.sheet_id}
          newSheetId={sheet.new.sheet_id}
          dpi={sheet.dpi}
          mode="overlay"
          controls={false}
          changeRegions={regions}
          selectedRegion={selected?.index ?? null}
          onSelectRegion={selectRegion}
        />
      </div>

      {selected && <RegionFacts region={selected} />}
    </aside>
  );
}

function RegionFacts({ region }: { region: ChangeRegion }) {
  return (
    <dl className="changes__facts tabular">
      <div>
        <dt>What changed</dt>
        <dd className="changes__facts-what">{region.explanation}</dd>
      </div>
      {region.old_text !== null && (
        <div>
          <dt>Was</dt>
          <dd>{region.old_text || '—'}</dd>
        </div>
      )}
      {region.new_text !== null && (
        <div>
          <dt>Now</dt>
          <dd>{region.new_text || '—'}</dd>
        </div>
      )}
      <div>
        <dt>Area on paper</dt>
        <dd>{fmtMm(region.area_mm2, 1)}²</dd>
      </div>
      <div>
        <dt>Area at scale</dt>
        <dd>{fmtSiteArea(region.area_site_mm2)}</dd>
      </div>
      {region.moved_by_mm !== null && (
        <div>
          <dt>Moved by</dt>
          <dd>
            {fmtMm(region.moved_by_mm)}
            {region.moved_by_site_mm !== null &&
              ` (${(region.moved_by_site_mm / 1000).toFixed(2)} m at scale)`}
          </dd>
        </div>
      )}
    </dl>
  );
}

// ── The other three states ─────────────────────────────────────────────

function EmptyView() {
  const start = useCompareStore((store) => store.start);
  const go = useAppStore((store) => store.go);

  return (
    <main className="changes changes--message">
      <div className="changes__message">
        <h1 className="changes__title">Changes</h1>
        <p className="changes__hint">
          Nothing has been compared yet. Comparing matches the two issues,
          aligns each pair, and then finds what differs — you do not need to
          visit those stages first.
        </p>
        <div className="changes__message-actions">
          <button
            type="button"
            className="button button--primary"
            onClick={() => void start()}
          >
            Compare the drawings
          </button>
          <button type="button" className="changes__link" onClick={() => go('setup')}>
            Change the folders first
          </button>
        </div>
      </div>
    </main>
  );
}

function RunningView() {
  const state = useCompareStore();
  const cancel = useCompareStore((store) => store.cancel);
  const { current, total, current_label: label } = state.progress;
  const percent = total > 0 ? Math.round((current / total) * 100) : 0;

  return (
    <main className="changes changes--message">
      <div className="changes__message">
        <h1 className="changes__title">
          {state.phase === 'aligning' ? 'Aligning the drawings' : 'Finding the changes'}
        </h1>
        <p className="changes__hint" role="status">
          {state.message ?? 'Working…'}
        </p>
        <div
          className="changes__progress"
          role="progressbar"
          aria-valuenow={percent}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="changes__progress-fill" style={{ width: `${percent}%` }} />
        </div>
        <p className="changes__progress-text micro tabular">
          {current} of {total}
          {label ? ` · ${label}` : ''}
        </p>
        <button type="button" className="button" onClick={() => void cancel()}>
          Stop
        </button>
      </div>
    </main>
  );
}

function FailedView() {
  const state = useCompareStore();
  const start = useCompareStore((store) => store.start);

  return (
    <main className="changes changes--message">
      <div className="changes__message">
        <h1 className="changes__title">The comparison did not finish</h1>
        <p className="changes__hint">{state.error ?? 'The engine did not say why.'}</p>
        <button type="button" className="button button--primary" onClick={() => void start()}>
          Try again
        </button>
      </div>
    </main>
  );
}
