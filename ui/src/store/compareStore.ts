/**
 * State for the Changes screen (Phase 5).
 *
 * The run lives on the engine, like matching, rename and alignment: this
 * store starts it, polls until it settles, then loads the change list. A
 * generation counter makes sure a poll that returns after the screen
 * unmounted (or after a newer run started) never overwrites newer state.
 *
 * `start()` is the standalone compare tool. The engine may answer that it
 * had to align first, in which case this store follows the alignment run to
 * its end and then asks again — so the user presses one button and gets a
 * change list, rather than walking a wizard.
 *
 * Triage (confirm / dismiss) is a LOCAL, session-only decision for now,
 * matching how the alignment screen keeps its accept and exclude notes.
 */

import { create } from 'zustand';

import {
  ApiError,
  cancelCompare,
  fetchAlignStatus,
  fetchCompareResults,
  fetchCompareStatus,
  startCompare,
} from '../api/client';
import type {
  ChangeRegion,
  ChangeSeverity,
  CompareResultRow,
  CompareStatus,
} from '../api/types';
import type { StatusTone } from '../components/StatusPill';

/** How often the screen asks where the run has got to. */
const POLL_INTERVAL_MS = 700;

/** Where a run is: the engine's own lifecycle words, kept verbatim. */
export type ComparePhase = 'idle' | 'aligning' | 'running' | 'done' | 'failed' | 'cancelled';

/** What the change list filters to. */
export type ChangeFilter = ChangeSeverity | 'all';

export interface CompareProgress {
  current: number;
  total: number;
  current_label: string | null;
}

/** One change, with the sheet it sits on — the flat list the screen walks. */
export interface ChangeEntry {
  region: ChangeRegion;
  /** Index into `results`. */
  sheetIndex: number;
  sheet: CompareResultRow;
}

interface CompareState {
  phase: ComparePhase;
  progress: CompareProgress;
  /** Engine progress copy while running; outcome copy once settled. */
  message: string | null;
  /** Engine errors, shown verbatim. */
  error: string | null;
  /** change type → count, as the engine reported it. */
  summary: Record<string, number>;
  /** Engine row order: one row per compared pair. */
  results: CompareResultRow[];
  /** Which sheet's changes are showing. */
  selectedSheet: number | null;
  /** Which change is highlighted on that sheet. */
  selectedRegion: number | null;
  filter: ChangeFilter;
  /** Show the changes the engine judged presentation-only. */
  showCosmetic: boolean;
  /** "sheetIndex:regionIndex" of changes the user confirmed or dismissed. */
  confirmed: Set<string>;
  dismissed: Set<string>;

  enter: () => Promise<void>;
  /** The standalone tool: align if needed, then compare. */
  start: () => Promise<void>;
  refreshResults: () => Promise<void>;
  cancel: () => Promise<void>;
  selectSheet: (index: number | null) => void;
  selectRegion: (index: number | null) => void;
  setFilter: (filter: ChangeFilter) => void;
  toggleCosmetic: () => void;
  confirm: (sheetIndex: number, regionIndex: number) => void;
  dismiss: (sheetIndex: number, regionIndex: number) => void;
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

export function triageKey(sheetIndex: number, regionIndex: number): string {
  return `${sheetIndex}:${regionIndex}`;
}

// ── The poll loop: one generation at a time ────────────────────────────

let pollGeneration = 0;
let entryPromise: Promise<void> | null = null;

function invalidatePolls(): void {
  pollGeneration += 1;
}

function write(
  partial: Partial<CompareState> | ((state: CompareState) => Partial<CompareState>),
): void {
  useCompareStore.setState(partial);
}

async function loadResults(generation: number): Promise<void> {
  try {
    const payload = await fetchCompareResults();
    if (generation !== pollGeneration) return;
    write({ results: payload.results, summary: payload.summary, error: null });
  } catch (error) {
    if (generation !== pollGeneration) return;
    write({ error: message(error) });
  }
}

/**
 * Follow the alignment run to its end, then ask the engine to compare.
 *
 * Alignment is a step the compare tool needs, not a screen the user has to
 * visit, so its progress is reported here in the compare screen's own words.
 */
async function followAlignment(generation: number): Promise<void> {
  for (;;) {
    await delay(POLL_INTERVAL_MS);
    if (generation !== pollGeneration) return;

    let status;
    try {
      status = await fetchAlignStatus();
    } catch (error) {
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
      message: status.message ? `Aligning first — ${status.message}` : 'Aligning first…',
      error: null,
    });

    if (status.state === 'running') continue;
    if (status.state === 'done') {
      await beginCompare(generation);
      return;
    }
    write({
      phase: status.state === 'cancelled' ? 'cancelled' : 'failed',
      error:
        status.state === 'failed'
          ? (status.error ?? 'The drawings could not be aligned, so nothing can be compared.')
          : null,
    });
    return;
  }
}

/** Ask the engine to compare, and follow that run. */
async function beginCompare(generation: number): Promise<void> {
  try {
    const response = await startCompare();
    if (generation !== pollGeneration) return;
    if (response.stage === 'aligning') {
      write({ phase: 'aligning', message: 'Aligning first…' });
      await followAlignment(generation);
      return;
    }
    write({ phase: 'running', message: 'Comparing…', error: null });
    await pollLoop(generation);
  } catch (error) {
    if (generation !== pollGeneration) return;
    write({ phase: 'failed', error: message(error) });
  }
}

/** Fetch status every 700 ms until the run settles, then load the rows. */
async function pollLoop(generation: number): Promise<void> {
  for (;;) {
    await delay(POLL_INTERVAL_MS);
    if (generation !== pollGeneration) return;

    let status: CompareStatus;
    try {
      status = await fetchCompareStatus();
    } catch (error) {
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

    if (status.state === 'done' || status.state === 'cancelled') {
      write({
        phase: status.state === 'done' ? 'done' : 'cancelled',
        summary: status.summary,
      });
      await loadResults(pollGeneration);
      return;
    }
    if (status.state === 'failed') {
      write({
        phase: 'failed',
        error: status.error ?? status.message ?? 'The comparison failed.',
      });
      return;
    }
    write({ phase: 'idle', error: 'The comparison was interrupted before it finished.' });
    return;
  }
}

/**
 * Adopt whatever the engine is already doing.
 *
 * Deliberately writes without checking the poll generation: React's strict
 * mode mounts the screen twice, and the unmount in between bumps the
 * counter. Gating these writes on the generation captured at entry meant a
 * perfectly good result arrived and was thrown away, and the screen sat
 * there claiming nothing had been compared.
 */
async function reconcile(): Promise<void> {
  let status: CompareStatus;
  try {
    status = await fetchCompareStatus();
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
  if (status.state === 'done' || status.state === 'cancelled') {
    write({
      phase: status.state === 'done' ? 'done' : 'cancelled',
      summary: status.summary,
      message: status.message,
    });
    await loadResults(pollGeneration);
    return;
  }
  if (status.state === 'failed') {
    write({
      phase: 'failed',
      error: status.error ?? status.message ?? 'The comparison failed.',
    });
    return;
  }
  write({ phase: 'idle', message: status.message, error: null });
}

function startPolling(): void {
  invalidatePolls();
  void pollLoop(pollGeneration);
}

export const useCompareStore = create<CompareState>((set, get) => ({
  phase: 'idle',
  progress: { current: 0, total: 0, current_label: null },
  message: null,
  error: null,
  summary: {},
  results: [],
  selectedSheet: null,
  selectedRegion: null,
  filter: 'all',
  showCosmetic: false,
  confirmed: new Set<string>(),
  dismissed: new Set<string>(),

  enter: async () => {
    if (entryPromise) return entryPromise;
    entryPromise = reconcile();
    try {
      await entryPromise;
    } finally {
      entryPromise = null;
    }
  },

  start: async () => {
    invalidatePolls();
    const generation = pollGeneration;
    set({
      phase: 'running',
      message: 'Starting…',
      error: null,
      results: [],
      summary: {},
      selectedSheet: null,
      selectedRegion: null,
      progress: { current: 0, total: 0, current_label: null },
    });
    await beginCompare(generation);
  },

  refreshResults: async () => {
    await loadResults(pollGeneration);
  },

  cancel: async () => {
    try {
      await cancelCompare();
    } catch (error) {
      set({ error: message(error) });
    }
  },

  selectSheet: (index) => set({ selectedSheet: index, selectedRegion: null }),
  selectRegion: (index) => set({ selectedRegion: index }),
  setFilter: (filter) => set({ filter }),
  toggleCosmetic: () => set({ showCosmetic: !get().showCosmetic }),

  confirm: (sheetIndex, regionIndex) => {
    const key = triageKey(sheetIndex, regionIndex);
    const confirmed = new Set(get().confirmed);
    const dismissed = new Set(get().dismissed);
    dismissed.delete(key);
    if (confirmed.has(key)) confirmed.delete(key);
    else confirmed.add(key);
    set({ confirmed, dismissed });
  },

  dismiss: (sheetIndex, regionIndex) => {
    const key = triageKey(sheetIndex, regionIndex);
    const confirmed = new Set(get().confirmed);
    const dismissed = new Set(get().dismissed);
    confirmed.delete(key);
    if (dismissed.has(key)) dismissed.delete(key);
    else dismissed.add(key);
    set({ confirmed, dismissed });
  },

  leave: () => {
    invalidatePolls();
  },
}));

// ── Pure helpers the screen renders with ───────────────────────────────

/** How a severity reads as a status pill. */
export function severityTone(severity: ChangeSeverity): StatusTone {
  if (severity === 'critical') return 'danger';
  if (severity === 'major') return 'warn';
  if (severity === 'minor') return 'info';
  return 'neutral';
}

export const SEVERITY_LABEL: Record<ChangeSeverity, string> = {
  critical: 'Critical',
  major: 'Major',
  minor: 'Minor',
  trivial: 'Trivial',
};

export const CHANGE_TYPE_LABEL: Record<string, string> = {
  added: 'Added',
  removed: 'Removed',
  moved: 'Moved',
  modified: 'Modified',
  cosmetic: 'Cosmetic',
};

/** Rank order for sorting: worst first. */
const SEVERITY_RANK: Record<ChangeSeverity, number> = {
  critical: 0,
  major: 1,
  minor: 2,
  trivial: 3,
};

/**
 * Every change on every sheet, worst first.
 *
 * Cosmetic changes are hidden by default — the plan's answer to false
 * positives is to default them out of sight, not to drop them.
 */
export function allChanges(state: CompareState): ChangeEntry[] {
  const entries: ChangeEntry[] = [];
  state.results.forEach((sheet, sheetIndex) => {
    sheet.regions.forEach((region) => {
      if (!state.showCosmetic && region.is_cosmetic) return;
      if (state.filter !== 'all' && region.severity !== state.filter) return;
      entries.push({ region, sheetIndex, sheet });
    });
  });
  entries.sort(
    (a, b) => SEVERITY_RANK[a.region.severity] - SEVERITY_RANK[b.region.severity],
  );
  return entries;
}

/** Changes on one sheet, in reading order (the engine's own order). */
export function changesForSheet(state: CompareState, sheetIndex: number): ChangeRegion[] {
  const sheet = state.results[sheetIndex];
  if (!sheet) return [];
  return sheet.regions.filter((region) => {
    if (!state.showCosmetic && region.is_cosmetic) return false;
    if (state.filter !== 'all' && region.severity !== state.filter) return false;
    return true;
  });
}

/** Counts per severity across the whole run, for the filter chips. */
export function severityCounts(state: CompareState): Record<ChangeSeverity, number> {
  const counts: Record<ChangeSeverity, number> = {
    critical: 0,
    major: 0,
    minor: 0,
    trivial: 0,
  };
  for (const sheet of state.results) {
    for (const region of sheet.regions) {
      if (!state.showCosmetic && region.is_cosmetic) continue;
      counts[region.severity] += 1;
    }
  }
  return counts;
}

/** The one-line answer: how many sheets changed, and how many changes. */
export function headline(state: CompareState): string {
  if (state.results.length === 0) return 'Nothing has been compared yet.';
  const changedSheets = state.results.filter((row) => row.substantive_count > 0).length;
  const total = state.results.reduce((sum, row) => sum + row.substantive_count, 0);
  if (total === 0) {
    return `No changes found on ${state.results.length} compared ${
      state.results.length === 1 ? 'sheet' : 'sheets'
    }.`;
  }
  return `${total} ${total === 1 ? 'change' : 'changes'} on ${changedSheets} of ${
    state.results.length
  } ${state.results.length === 1 ? 'sheet' : 'sheets'}.`;
}
