/**
 * State for the Alignment review screen (Phase 4, Task 4.14) and the Manual
 * alignment screen (Task 4.13).
 *
 * The batch run lives on the engine, exactly like matching and rename: this
 * store starts it, polls `/api/align/status` every ~700 ms until the run
 * settles, then loads `/api/align/results`. A generation counter makes sure
 * a poll that returns after the screen unmounted (or after a newer run
 * started) never overwrites newer state.
 *
 * `results` keeps the ENGINE's row order (one row per accepted pair, in run
 * order) because the manual endpoint replaces a row by that index. The list
 * is only ever reordered by the pure helpers when it is rendered.
 *
 * Accept and exclude are LOCAL, session-only decisions — the kind of note
 * the review screen keeps so Phase 5 can act on it later. Nothing here is
 * posted back to the engine; only `run`, `cancel` and manual alignment ever
 * talk to it.
 */

import { create } from 'zustand';

import {
  ApiError,
  cancelAlignment,
  fetchAlignResults,
  fetchAlignStatus,
  runAlignment,
} from '../api/client';
import type { AlignResultRow, AlignStatus, AlignVerdict } from '../api/types';
import type { StatusTone } from '../components/StatusPill';

/** How often the screen asks where the batch run has got to. */
const POLL_INTERVAL_MS = 700;

/** Where a run is: the engine's own lifecycle words, kept verbatim. */
export type AlignPhase = 'idle' | 'running' | 'done' | 'failed' | 'cancelled';

/** The three summary groups the chips and the header sentence talk in. */
export type AlignGroup = 'aligned' | 'review' | 'failed';

/**
 * What the result list filters to. The chips filter by group (`aligned` =
 * excellent + good); `excluded` shows the rows the user excluded locally so
 * an exclusion can be undone.
 */
export type AlignFilter = AlignGroup | 'excluded' | null;

/** One row of the results list with the index it holds in `results`. */
export interface AlignEntry {
  row: AlignResultRow;
  index: number;
}

export interface AlignProgress {
  current: number;
  total: number;
  current_label: string | null;
}

interface AlignState {
  /** Engine lifecycle of the current (or last) batch run. */
  phase: AlignPhase;
  progress: AlignProgress;
  /** Engine progress copy while running; outcome copy once settled. */
  message: string | null;
  /** Engine errors, shown verbatim. */
  error: string | null;
  /** Transient outcome lines (accept, exclude, manual apply…). */
  notice: string | null;
  /** verdict → count, as the engine reported it. */
  summary: Record<string, number>;
  /** Engine row order — index i matches POST /api/align/manual index i. */
  results: AlignResultRow[];
  /** Which row's detail is open (an index into `results`). */
  selectedIndex: number | null;
  filters: { verdict: AlignFilter };
  /** Rows excluded from the comparison — a local session decision. */
  excluded: Set<number>;
  /** Rows accepted for comparison — a local session decision. */
  accepted: Set<number>;

  /** On mount: adopt whatever the engine is doing, then keep it current. */
  enter: () => Promise<void>;
  /** POST a fresh run of the accepted pairs, then poll it to the end. */
  run: () => Promise<void>;
  /** Re-fetch the result rows (used when a run reaches `done`). */
  refreshResults: () => Promise<void>;
  /** Ask the engine to stop the run cleanly; polling continues. */
  cancel: () => Promise<void>;
  /** Open (or close) a row's detail. Indexes into `results`. */
  selectRow: (index: number | null) => void;
  /** Cycle a summary chip / the excluded view on and off. */
  setFilter: (filter: AlignFilter) => void;
  toggleExclude: (index: number) => void;
  toggleAccept: (index: number) => void;
  /** The manual screen's POST succeeded: swap that engine row in place. */
  applyManualRow: (row: AlignResultRow) => void;
  setNotice: (notice: string | null) => void;
  /** Stop the poller when the screen unmounts; the engine run continues. */
  leave: () => void;
}

function message(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Something went wrong. The details are in the log file.';
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// ── The poll loop: one generation at a time ────────────────────────────

/** Bumped when a newer run or an unmount makes older polls stale. */
let pollGeneration = 0;
/** Single-flight entry, so a doubled mount never starts two reconciles. */
let entryPromise: Promise<void> | null = null;

function invalidatePolls(): void {
  pollGeneration += 1;
}

function write(partial: Partial<AlignState> | ((state: AlignState) => Partial<AlignState>)): void {
  useAlignStore.setState(partial);
}

async function loadResults(generation: number): Promise<void> {
  try {
    const payload = await fetchAlignResults();
    if (generation !== pollGeneration) return;
    write({ results: payload.results, summary: payload.summary, error: null });
  } catch (error) {
    if (generation !== pollGeneration) return;
    // The engine only serves results once the run is `done`; a 422 here is a
    // genuine race worth showing, never a crash.
    write({ error: message(error) });
  }
}

/** Fetch status every 700 ms until the run settles, then load the rows. */
async function pollLoop(generation: number): Promise<void> {
  for (;;) {
    await delay(POLL_INTERVAL_MS);
    if (generation !== pollGeneration) return;

    let status: AlignStatus;
    try {
      status = await fetchAlignStatus();
    } catch (error) {
      // The engine may briefly refuse while the run warms up; show why but
      // keep asking. A later poll clears the message again.
      if (generation === pollGeneration) write({ error: message(error) });
      continue;
    }
    if (generation !== pollGeneration) return;

    write({
      progress: {
        current: status.current,
        total: status.total,
        current_label: status.current_label,
      },
      message: status.message,
      error: null,
    });

    if (status.state === 'running') continue;

    if (status.state === 'done') {
      write({ phase: 'done', summary: status.summary });
      // Load with the LIVE generation: a strict-mode remount that bumped the
      // counter mid-flight must not silently drop an otherwise-good result.
      await loadResults(pollGeneration);
      return;
    }
    if (status.state === 'cancelled') {
      // The engine keeps a partial run, but it only serves rows once a run
      // is `done`, so the honest view is: stopped, run again when ready.
      write({ phase: 'cancelled', error: null });
      return;
    }
    if (status.state === 'failed') {
      write({
        phase: 'failed',
        error: status.error ?? status.message ?? 'The alignment run failed.',
      });
      return;
    }
    // idle: the engine session was reset while the run was going.
    write({ phase: 'idle', error: 'The alignment run was interrupted before it finished.' });
    return;
  }
}

function startPolling(): void {
  invalidatePolls();
  const generation = pollGeneration;
  void pollLoop(generation);
}

/** Adopt the engine's current state on screen mount (single flight). */
async function reconcile(): Promise<void> {
  let status: AlignStatus;
  try {
    status = await fetchAlignStatus();
  } catch (error) {
    write({ error: message(error) });
    return;
  }

  if (status.state === 'running') {
    write({
      phase: 'running',
      progress: {
        current: status.current,
        total: status.total,
        current_label: status.current_label,
      },
      message: status.message,
    });
    startPolling();
    return;
  }
  if (status.state === 'done') {
    write({ phase: 'done', summary: status.summary });
    await loadResults(pollGeneration);
    return;
  }  if (status.state === 'failed') {
    write({
      phase: 'failed',
      error: status.error ?? status.message ?? 'The alignment run failed.',
    });
    return;
  }
  if (status.state === 'cancelled') {
    write({ phase: 'cancelled', error: null });
    return;
  }
  write({ phase: 'idle', message: status.message, error: null });
}

// ── The store ──────────────────────────────────────────────────────────

export const useAlignStore = create<AlignState>((set, get) => ({
  phase: 'idle',
  progress: { current: 0, total: 0, current_label: null },
  message: null,
  error: null,
  notice: null,
  summary: {},
  results: [],
  selectedIndex: null,
  filters: { verdict: null },
  excluded: new Set<number>(),
  accepted: new Set<number>(),

  enter: async () => {
    if (entryPromise) return entryPromise;
    entryPromise = reconcile();
    try {
      await entryPromise;
    } finally {
      entryPromise = null;
    }
  },

  run: async () => {
    if (get().phase === 'running') return;
    invalidatePolls();
    write({ error: null, notice: null });
    try {
      await runAlignment();
    } catch (error) {
      // The engine refuses without accepted pairs; its words are the message.
      write({ phase: 'idle', error: message(error) });
      return;
    }
    write({
      phase: 'running',
      progress: { current: 0, total: 0, current_label: null },
      message: null,
      results: [],
      summary: {},
      selectedIndex: null,
      excluded: new Set<number>(),
      accepted: new Set<number>(),
    });
    startPolling();
  },

  refreshResults: async () => {
    await loadResults(pollGeneration);
  },

  cancel: async () => {
    try {
      await cancelAlignment();
      // The engine flips to `cancelled` once its worker notices; the poller
      // that is already running reports that state. Nothing to do here.
    } catch (error) {
      write({ error: message(error) });
    }
  },

  selectRow: (selectedIndex) => set({ selectedIndex }),

  setFilter: (verdict) =>
    set((state) => ({
      filters: { verdict: state.filters.verdict === verdict ? null : verdict },
    })),

  toggleExclude: (index) =>
    set((state) => {
      const excluded = new Set(state.excluded);
      const accepted = new Set(state.accepted);
      const excluding = !excluded.has(index);
      if (excluding) {
        excluded.add(index);
        accepted.delete(index);
      } else {
        excluded.delete(index);
      }
      return {
        excluded,
        accepted,
        notice: excluding
          ? 'Excluded from the comparison set — a local choice for this session only.'
          : 'Included in the comparison set again.',
      };
    }),

  toggleAccept: (index) =>
    set((state) => {
      const accepted = new Set(state.accepted);
      const accepting = !accepted.has(index);
      if (accepting) accepted.add(index);
      else accepted.delete(index);
      return {
        accepted,
        notice: accepting
          ? 'Accepted for comparison — a local choice for this session only.'
          : 'Acceptance withdrawn.',
      };
    }),

  applyManualRow: (row) =>
    set((state) => {
      const index = state.selectedIndex;
      if (index === null || index < 0 || index >= state.results.length) return {};
      const results = [...state.results];
      results[index] = row;
      return {
        results,
        notice: 'Manual alignment applied — the pair now shows the new result.',
      };
    }),

  setNotice: (notice) => set({ notice }),

  leave: () => invalidatePolls(),
}));

// ── Pure helpers ────────────────────────────────────────────────────────

/** How the summary groups a verdict (the chips and the sentence). */
export function verdictGroup(verdict: AlignVerdict): AlignGroup {
  if (verdict === 'excellent' || verdict === 'good') return 'aligned';
  if (verdict === 'poor') return 'review';
  return 'failed';
}

/** Status-pill tone of one verdict: ok, warn, danger — never brand red. */
export function verdictTone(verdict: AlignVerdict): StatusTone {
  if (verdict === 'excellent' || verdict === 'good') return 'ok';
  if (verdict === 'poor') return 'warn';
  return 'danger';
}

/** Reading of one verdict in the row list, sentence case. */
export const VERDICT_LABEL: Record<AlignVerdict, string> = {
  excellent: 'Excellent',
  good: 'Good',
  poor: 'Poor',
  failed: 'Could not align',
};

function verdictRank(verdict: AlignVerdict): number {
  if (verdict === 'failed') return 0;
  if (verdict === 'poor') return 1;
  // excellent and good are both "aligned"; error decides between them.
  return 2;
}

function rmsCompare(left: number | null, right: number | null): number {
  // Descending — the worst fit first. Null (no assessment) sorts last.
  const a = left ?? -Infinity;
  const b = right ?? -Infinity;
  return b - a;
}

/** A stable sort: failures first, then poor, then by error, worst first. */
export function sortedRows(rows: AlignResultRow[]): AlignResultRow[] {
  return [...rows].sort((left, right) => {
    const byVerdict = verdictRank(left.verdict) - verdictRank(right.verdict);
    if (byVerdict !== 0) return byVerdict;
    return rmsCompare(left.rms_mm_on_paper, right.rms_mm_on_paper);
  });
}

/** The rows the list shows: chip filter + exclusion applied, then sorted. */
export function visibleRows(state: AlignState): AlignEntry[] {
  const filter = state.filters.verdict;
  const entries: AlignEntry[] = [];
  for (let index = 0; index < state.results.length; index++) {
    const row = state.results[index];
    if (!row) continue;
    const isExcluded = state.excluded.has(index);
    if (filter === 'excluded') {
      if (!isExcluded) continue;
    } else {
      if (isExcluded) continue;
      if (filter !== null && verdictGroup(row.verdict) !== filter) continue;
    }
    entries.push({ row, index });
  }
  return sortedEntries(entries);
}

/** Sort `{row,index}` entries the same way `sortedRows` sorts rows. */
export function sortedEntries(entries: AlignEntry[]): AlignEntry[] {
  return [...entries].sort((left, right) => {
    const byVerdict = verdictRank(left.row.verdict) - verdictRank(right.row.verdict);
    if (byVerdict !== 0) return byVerdict;
    return rmsCompare(left.row.rms_mm_on_paper, right.row.rms_mm_on_paper);
  });
}

/** Per-verdict counts over a row set; every verdict key exists. */
export function countsByVerdict(rows: AlignResultRow[]): Record<AlignVerdict, number> {
  const counts: Record<AlignVerdict, number> = {
    excellent: 0,
    good: 0,
    poor: 0,
    failed: 0,
  };
  for (const row of rows) counts[row.verdict] += 1;
  return counts;
}

/** Counts per summary group, from rows in any order. */
export function countsByGroup(rows: AlignResultRow[]): Record<AlignGroup, number> {
  const counts: Record<AlignGroup, number> = { aligned: 0, review: 0, failed: 0 };
  for (const row of rows) counts[verdictGroup(row.verdict)] += 1;
  return counts;
}

/** The header sentence: `N aligned · M need review · K could not align`. */
export function summarySentence(rows: AlignResultRow[]): string {
  const counts = countsByGroup(rows);
  return (
    `${counts.aligned} aligned · ${counts.review} ` +
    `${counts.review === 1 ? 'needs' : 'need'} review · ` +
    `${counts.failed} could not align`
  );
}

/** The row the manual screen acts on: `selectedIndex` mapped to its row. */
export function manualTarget(state: AlignState): { row: AlignResultRow; index: number } | null {
  const index = state.selectedIndex;
  if (index === null) return null;
  const row = state.results[index];
  return row === undefined ? null : { row, index };
}

/** A human reading of the engine's method string, e.g. "text_anchors". */
export function methodLabel(method: string): string {
  const labels: Record<string, string> = {
    text_anchors: 'Unique text',
    grid_bubbles: 'Grid bubbles',
    phase_correlation: 'Phase correlation',
    features: 'Image features',
    sheet_border: 'Sheet frame',
    manual: 'Manual points',
    ecc_refinement: 'ECC refinement',
    similarity: 'Similarity fit',
    affine: 'Affine fit',
  };
  return labels[method] ?? method;
}

/** Readable name of one quality metric, sentence case. */
export function metricLabel(name: string): string {
  const labels: Record<string, string> = {
    rms_residual_px: 'Average error',
    inlier_ratio: 'Inlier agreement',
    anchor_count: 'Reference points',
    anchor_spread: 'Anchor spread',
    transform_sanity: 'Transform sanity',
    ink_overlap: 'Ink overlap',
    holdout_rms: 'Held-out error',
  };
  return labels[name] ?? name;
}
