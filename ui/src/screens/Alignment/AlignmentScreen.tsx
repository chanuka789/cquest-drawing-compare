/**
 * Screen 5 — Alignment review (Phase 4, Task 4.14).
 *
 * A batch view of one alignment result per accepted match pair. Failures
 * and poor fits sort to the top, because they are the reason to open this
 * screen at all; every count in the summary bar is a filter chip.
 *
 * Clicking a row opens the detail aside: the overlay preview, the plain
 * English explanation, every quality-gate metric against its threshold, and
 * the per-pair actions. Accept and exclude are LOCAL, session-only decisions
 * (nothing here is posted to the engine); 'Align manually' and 'Re-run
 * alignment' are the only actions that talk to it.
 *
 * While a run is going the screen shows its progress rail and a cancel that
 * works. Engine errors are shown verbatim.
 */

import { useEffect, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import { Lightbox } from '../../components/Lightbox';
import { ProgressRail } from '../../components/ProgressRail';
import { StatusPill } from '../../components/StatusPill';
import type { StatusTone } from '../../components/StatusPill';
import type { ProgressEvent } from '../../api/types';
import type { AlignMetric, AlignResultRow } from '../../api/types';
import { useAppStore } from '../../store/appStore';
import {
  countsByGroup,
  methodLabel,
  metricLabel,
  summarySentence,
  useAlignStore,
  VERDICT_LABEL,
  verdictTone,
  visibleRows,
} from '../../store/alignStore';
import type { AlignGroup } from '../../store/alignStore';

import './AlignmentScreen.css';

const ROW_HEIGHT = 44;

/** One chip of the summary bar: a quality group, a reading, a tone. */
interface GroupChip {
  group: AlignGroup;
  label: string;
  tone: StatusTone;
}

const GROUP_CHIPS: GroupChip[] = [
  { group: 'aligned', label: 'aligned', tone: 'ok' },
  { group: 'review', label: 'needs review', tone: 'warn' },
  { group: 'failed', label: 'could not align', tone: 'danger' },
];

/** Chip copy that agrees with its count ("1 needs review" vs "2 need review"). */
function groupChipLabel(chip: GroupChip, count: number): string {
  if (chip.group === 'review') return count === 1 ? 'needs review' : 'need review';
  return chip.label;
}

/** A row-major 3×3 matrix → the Lightbox's flat 9-element form. */
function flattenMatrix(matrix: number[][] | null): number[] | null {
  if (matrix === null || matrix.length !== 3) return null;
  const flat: number[] = [];
  for (const row of matrix) {
    if (!row || row.length !== 3) return null;
    flat.push(...row);
  }
  return flat;
}

function fmtMm(value: number | null, digits = 2): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value.toFixed(digits)} mm`;
}

function fmtSeconds(value: number): string {
  return `${value.toFixed(1)} s`;
}

/** CSV cells: quote anything carrying a comma, quote or line break. */
function csvCell(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** Client-side report export — no engine call, useful for Part D reports. */
function exportReport(rows: AlignResultRow[]): void {
  const header = [
    'Previous issue file',
    'Current issue file',
    'Verdict',
    'Method',
    'RMS on site (mm)',
    'Explanation',
  ];
  const lines = rows.map((row) =>
    [
      row.old.filename,
      row.new.filename,
      row.verdict,
      methodLabel(row.method),
      row.rms_mm_on_site === null || !Number.isFinite(row.rms_mm_on_site)
        ? ''
        : row.rms_mm_on_site.toFixed(3),
      row.explanation,
    ].map(csvCell),
  );
  // The BOM makes Excel read the file as UTF-8.
  const csv = `\uFEFF${[header, ...lines].map((line) => line.join(',')).join('\r\n')}`;
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'alignment-report.csv';
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ── The screen ─────────────────────────────────────────────────────────

export function AlignmentScreen() {
  const state = useAlignStore();
  const goToMatching = useAppStore((store) => store.goToMatching);

  useEffect(() => {
    void state.enter();
    return () => state.leave();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const running = state.phase === 'running';
  const empty = state.results.length === 0;

  return (
    <div className="align">
      <header className="align__head">
        <div className="align__title-row">
          <button type="button" className="align__back" onClick={goToMatching}>
            ← Matching
          </button>
          <h1>Alignment</h1>
          <div className="align__head-actions">
            {!empty && !running && (
              <button
                type="button"
                className="align__export"
                onClick={() => exportReport(state.results)}
              >
                Export report
              </button>
            )}
            {empty && (
              <button
                type="button"
                className="button button--primary"
                onClick={() => void state.run()}
                disabled={running}
                title={running ? 'An alignment run is in progress' : undefined}
              >
                {running
                  ? 'Run alignment…'
                  : state.phase === 'failed' || state.phase === 'cancelled'
                    ? 'Run alignment again'
                    : 'Run alignment'}
              </button>
            )}
          </div>
        </div>

        <p className="align__sentence" role="status">
          {!empty && !running
            ? summarySentence(state.results.filter((_, index) => !state.excluded.has(index)))
            : emptySentence(state)}
        </p>
      </header>

      <Messages />

      {running ? (
        <RunningPanel />
      ) : empty ? (
        <EmptyPanel state={state} />
      ) : (
        <ResultsPanel />
      )}
    </div>
  );
}

/** The sentence shown before any results exist, by engine phase. */
function emptySentence(state: ReturnType<typeof useAlignStore.getState>): string {
  switch (state.phase) {
    case 'failed':
      return 'The alignment run failed — its message is shown below. You can run it again.';
    case 'cancelled':
      return 'Alignment was stopped before it finished. Run it again when you are ready.';
    default:
      return 'Runs the accepted pairs from Matching through the alignment cascade. Nothing has been aligned yet.';
  }
}

function Messages() {
  const state = useAlignStore();
  return (
    <>
      {state.notice && (
        <p className="align__notice" role="status">
          {state.notice}
        </p>
      )}
      {state.error && (
        <p className="align__error" role="alert">
          {state.error}
        </p>
      )}
    </>
  );
}

// ── Run states ─────────────────────────────────────────────────────────

function RunningPanel() {
  const progress = useAlignStore((state) => state.progress);
  const message = useAlignStore((state) => state.message);
  const cancel = useAlignStore((state) => state.cancel);

  const label = progress.current_label;
  const text =
    message ?? (label === null || label.length === 0 ? 'Aligning…' : `Aligning… ${label}`);
  const event: ProgressEvent = {
    run_id: '',
    stage: 'reconcile',
    kind: 'progress',
    current: progress.current,
    total: progress.total,
    current_item: text,
    elapsed: 0,
    message: text,
    fraction: progress.total > 0 ? progress.current / progress.total : 0,
    eta: null,
    detail: {},
  };

  return (
    <div className="align__run" aria-live="polite">
      <ProgressRail event={event} onCancel={() => void cancel()} />
      <p className="align__run-note micro">
        Alignment tries the strategy cascade on every pair and records why
        each attempt did not win. This usually takes a few seconds per pair.
      </p>
    </div>
  );
}

function EmptyPanel({ state }: { state: ReturnType<typeof useAlignStore.getState> }) {
  return (
    <div className="align__empty">
      {state.phase === 'failed' && state.error && (
        <p className="align__failed" role="alert">
          {state.error}
        </p>
      )}
      <p className="align__empty-copy micro">
        {state.phase === 'cancelled'
          ? 'Run alignment again when you are ready — it restarts from the same accepted pairs.'
          : state.phase === 'failed'
            ? 'The run failed before any pair finished. Try again; the engine reports the reason verbatim above.'
            : 'The engine aligns the pairs you accepted in Matching and scores every fit with the same quality gate.'}
      </p>
    </div>
  );
}

// ── The results view ───────────────────────────────────────────────────

function ResultsPanel() {
  const state = useAlignStore();
  const entries = visibleRows(state);
  const groupCounts = countsByGroup(
    state.results.filter((_, index) => !state.excluded.has(index)),
  );
  const excludedCount = state.excluded.size;
  const selectedIndex = state.selectedIndex;
  const selectedRow = selectedIndex === null ? null : (state.results[selectedIndex] ?? null);

  return (
    <>
      <div className="align__summary" role="group" aria-label="Filter by verdict">
        {GROUP_CHIPS.map((chip) => {
          const count = groupCounts[chip.group];
          if (count === 0 && state.filters.verdict !== chip.group) return null;
          return (
            <button
              key={chip.group}
              type="button"
              className={`align__chip${state.filters.verdict === chip.group ? ' align__chip--on' : ''}`}
              onClick={() => state.setFilter(chip.group)}
              aria-pressed={state.filters.verdict === chip.group}
            >
              <StatusPill tone={chip.tone}>
                <span className="tabular">{count}</span> {groupChipLabel(chip, count)}
              </StatusPill>
            </button>
          );
        })}
        {excludedCount > 0 && (
          <button
            type="button"
            className={`align__chip${state.filters.verdict === 'excluded' ? ' align__chip--on' : ''}`}
            onClick={() => state.setFilter('excluded')}
            aria-pressed={state.filters.verdict === 'excluded'}
          >
            <StatusPill tone="neutral">
              <span className="tabular">{excludedCount}</span> excluded
            </StatusPill>
          </button>
        )}
      </div>

      <div className="align__body">
        <ResultList entries={entries} />
        {selectedRow !== null && selectedIndex !== null && (
          <DetailAside row={selectedRow} index={selectedIndex} />
        )}
      </div>
    </>
  );
}

function ResultList({ entries }: { entries: Array<{ row: AlignResultRow; index: number }> }) {
  const state = useAlignStore();
  const selectRow = useAlignStore((store) => store.selectRow);
  const parentRef = useRef<HTMLDivElement>(null);
  // This list is deliberately virtualised like the Register and Rename
  // tables; the React Compiler lint's incompatibility note is a baseline
  // warning shared by every useVirtualizer call in this codebase.
  // oxlint-disable-next-line
  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  return (
    <div className="align__panel">
      <div className="align__cols micro">
        <span>Pair</span>
        <span>Method</span>
        <span>Verdict</span>
        <span className="align__col-right">RMS on site</span>
        <span className="align__col-right">Time</span>
      </div>
      <div className="align__scroll" ref={parentRef}>
        {entries.length === 0 && (
          <p className="align__none">
            {state.filters.verdict === 'excluded'
              ? 'Nothing is excluded right now — rows reappear under their verdict groups.'
              : 'No rows in this view yet.'}
          </p>
        )}
        <div
          className="align__canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const entry = entries[virtualRow.index];
            if (!entry) return null;
            const { row, index } = entry;
            const isSelected = state.selectedIndex === index;
            const isExcluded = state.excluded.has(index);
            const isAccepted = state.accepted.has(index);
            return (
              <div
                key={index}
                role="button"
                tabIndex={0}
                aria-current={isSelected ? 'true' : undefined}
                className={[
                  'align__row',
                  isSelected ? 'align__row--selected' : '',
                  isExcluded ? 'align__row--excluded' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
                onClick={() => selectRow(isSelected ? null : index)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    selectRow(isSelected ? null : index);
                  }
                }}
              >
                <span
                  className="align__pair"
                  title={`${row.old.filename} → ${row.new.filename}`}
                >
                  <span className="align__file">{row.old.filename}</span>
                  <span className="align__arrow" aria-hidden="true">
                    →
                  </span>
                  <span className="align__file">{row.new.filename}</span>
                  {isAccepted && (
                    <span className="align__local-note micro" title="A local note for this session only">
                      ✓ accepted
                    </span>
                  )}
                  {isExcluded && (
                    <span className="align__local-note micro" title="A local note for this session only">
                      excluded
                    </span>
                  )}
                </span>
                <span className="align__method micro" title={`Method: ${row.method}`}>
                  {methodLabel(row.method)}
                </span>
                <span className="align__verdict">
                  <StatusPill tone={verdictTone(row.verdict)}>{VERDICT_LABEL[row.verdict]}</StatusPill>
                </span>
                <span className="align__rms tabular">{fmtMm(row.rms_mm_on_site)}</span>
                <span className="align__time tabular">{fmtSeconds(row.duration_s)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── The detail aside ───────────────────────────────────────────────────

interface DetailAsideProps {
  row: AlignResultRow;
  index: number;
}

function DetailAside({ row, index }: DetailAsideProps) {
  const state = useAlignStore();
  const selectRow = useAlignStore((store) => store.selectRow);
  const toggleAccept = useAlignStore((store) => store.toggleAccept);
  const toggleExclude = useAlignStore((store) => store.toggleExclude);
  const run = useAlignStore((store) => store.run);
  const goToManual = useAppStore((store) => store.goToManual);
  const accepted = state.accepted.has(index);
  const excluded = state.excluded.has(index);
  const metrics = row.metrics ?? {};

  const openManual = () => {
    // The manual screen acts on `manualTarget()` — the row already selected.
    goToManual();
  };

  return (
    <aside className="align__aside" aria-label={`Details for ${row.old.filename}`}>
      <header className="align__aside-head">
        <div className="align__aside-title">
          <span className="align__aside-file" title={row.old.filename}>
            {row.old.filename}
          </span>
          <span className="align__aside-arrow" aria-hidden="true">
            →
          </span>
          <span className="align__aside-file" title={row.new.filename}>
            {row.new.filename}
          </span>
        </div>
        <button
          type="button"
          className="align__aside-close"
          onClick={() => selectRow(null)}
          aria-label="Close details"
        >
          ×
        </button>
      </header>

      <div className="align__aside-meta">
        <StatusPill tone={verdictTone(row.verdict)}>{VERDICT_LABEL[row.verdict]}</StatusPill>
        <StatusPill tone="neutral">{methodLabel(row.method)}</StatusPill>
        {accepted && (
          <StatusPill tone="neutral" title="A local note for this session only">
            Accepted locally
          </StatusPill>
        )}
        {excluded && (
          <StatusPill tone="neutral" title="A local note for this session only">
            Excluded locally
          </StatusPill>
        )}
      </div>

      {/* The pair, overlaid — the answer to "does it look aligned?" */}
      <div className="align__preview">
        {/* mode must be explicit: without it the Lightbox restores the user's
            remembered mode, and this detail panel always wants the overlay.
            controls=false keeps the viewer chrome for this screen. */}
        <Lightbox
          oldSheetId={row.old.sheet_id}
          newSheetId={row.new.sheet_id}
          transformMatrix={flattenMatrix(row.matrix)}
          dpi={200}
          mode="overlay"
          controls={false}
        />
        {row.matrix === null && (
          <p className="align__preview-note">
            No transform was fitted for this pair, so the sheets are shown
            unaligned.
          </p>
        )}
      </div>

      <dl className="align__facts tabular">
        <dt>RMS on paper</dt>
        <dd>{fmtMm(row.rms_mm_on_paper)}</dd>
        <dt>RMS on site</dt>
        <dd>{fmtMm(row.rms_mm_on_site)}</dd>
        <dt>Method</dt>
        <dd>{methodLabel(row.method)}</dd>
        <dt>Time</dt>
        <dd>{fmtSeconds(row.duration_s)}</dd>
      </dl>

      {row.explanation.length > 0 && (
        <p className="align__explanation">{row.explanation}</p>
      )}
      {row.note.length > 0 && (
        <p className="align__note micro">{row.note}</p>
      )}

      <div className="align__metrics">
        {Object.entries(metrics).map(([name, metric]) => (
          <MetricBar key={name} name={name} metric={metric} />
        ))}
      </div>

      <div className="align__actions">
        <button type="button" className="button button--primary align__action" onClick={openManual}>
          Align manually
        </button>
        <button
          type="button"
          className={`align__accept${accepted ? ' align__accept--on' : ''}`}
          onClick={() => toggleAccept(index)}
          aria-pressed={accepted}
          title="Local note for this session only — nothing is sent to the engine"
        >
          {accepted ? 'Accepted ✓' : 'Accept'}
        </button>
        <button
          type="button"
          className={`align__exclude${excluded ? ' align__exclude--on' : ''}`}
          onClick={() => toggleExclude(index)}
          aria-pressed={excluded}
          title="Local note for this session only — nothing is sent to the engine"
        >
          {excluded ? 'Include in comparison' : 'Exclude from comparison'}
        </button>
        <button type="button" className="align__rerun" onClick={() => void run()}>
          Re-run alignment
        </button>
      </div>
    </aside>
  );
}

// ── One metric bar ─────────────────────────────────────────────────────

/** How much of the track fills: the value against its threshold. */
function metricFill(value: number, threshold: number): number {
  if (!Number.isFinite(value) || !(threshold > 0)) return 0;
  return Math.min(Math.max(value / threshold, 0), 1);
}

/** "0.41 px · limit 2 px" — native units, NaN reads as '—'. */
function metricValueText(name: string, value: number): string {
  if (!Number.isFinite(value)) return '—';
  switch (name) {
    case 'anchor_count':
      return `${Math.round(value)}`;
    case 'transform_sanity':
      return value >= 0.5 ? 'Pass' : 'Fail';
    case 'inlier_ratio':
    case 'anchor_spread':
    case 'ink_overlap':
      return `${Math.round(value * 100)}%`;
    case 'rms_residual_px':
    case 'holdout_rms':
      return `${value.toFixed(2)} px`;
    default:
      return `${value.toFixed(2)}`;
  }
}

function MetricBar({ name, metric }: { name: string; metric: AlignMetric }) {
  const percent = Math.round(metricFill(metric.value, metric.threshold) * 100);
  const tone: StatusTone = metric.passed ? 'ok' : 'danger';
  return (
    <div className="align__metric">
      <div className="align__metric-head">
        <span className="align__metric-name micro">{metricLabel(name)}</span>
        <span className="align__metric-text tabular micro">
          {metricValueText(name, metric.value)} · limit {metricValueText(name, metric.threshold)}
        </span>
      </div>
      <div className="align__metric-track" aria-hidden="true">
        <span
          className={`align__metric-fill align__metric-fill--${tone}`}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
