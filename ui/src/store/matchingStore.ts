/**
 * State for the Matching review screen.
 *
 * Matching runs on the engine, not here. This store starts that run when the
 * screen opens, polls its progress, holds the result, and keeps every
 * decision the user has made about it. Decisions stay local until "Save
 * review", which posts them and then writes the audit file.
 */

import { create } from 'zustand';

import {
  ApiError,
  fetchMatchResult,
  finalizeMatch,
  getMatchStatus,
  postMatchDecisions,
  runMatch,
} from '../api/client';
import type { MatchPair, MatchResult, MatchRunState, MatchStatus } from '../api/types';

/** A manual decision: one old-unmatched sheet joined to one new sheet. */
export interface ManualPair {
  old_key: string;
  new_key: string;
}

/** A review pair's state once the user has decided it. */
export type Decision = 'accepted' | 'rejected' | 'manual' | null;

/** How often the screen asks the engine how the run is going. */
const POLL_INTERVAL_MS = 900;

/** Where the "accept all above" slider starts. */
export const DEFAULT_ACCEPT_THRESHOLD = 0.9;

type SetFn = (
  partial:
    | Partial<MatchingState>
    | ((state: MatchingState) => Partial<MatchingState>),
) => void;
type GetFn = () => MatchingState;

interface MatchingState {
  status: MatchRunState;
  runId: string | null;
  current: number;
  total: number;
  /** The status endpoint's message: progress copy, or a failure verbatim. */
  message: string | null;
  result: MatchResult | null;
  /** Engine errors shown verbatim. */
  error: string | null;
  notice: string | null;

  /** Section 1 (auto-matched) starts collapsed. */
  expandedAuto: boolean;
  selectedReviewIndex: number | null;
  /** The two sheets picked in the unmatched section, ready to pair. */
  manualOldKey: string | null;
  manualNewKey: string | null;
  /** Accept-all threshold, from the slider. */
  threshold: number;

  accepted: string[];
  rejected: string[];
  manual: ManualPair[];

  /** The engine's unresolved count after the last successful save, or null
   *  while the local decisions are newer than anything the engine knows. */
  savedUnresolved: number | null;

  saving: boolean;

  /** Called on mount: start a run if idle, poll, then fetch the result. */
  enter: () => Promise<void>;
  /** Re-fetch the result after a transient failure. */
  refresh: () => Promise<void>;
  /** Start over after a failure. */
  retry: () => Promise<void>;
  accept: (key: string) => void;
  reject: (key: string) => void;
  /** null clears the manual choice and puts the pair back to undecided. */
  chooseAlternative: (oldKey: string, newKey: string | null) => void;
  pairManually: (oldKey: string, newKey: string) => void;
  bulkAccept: (threshold: number) => void;
  moveReviewSelection: (delta: number) => void;
  selectReview: (index: number | null) => void;
  setManualOld: (key: string | null) => void;
  setManualNew: (key: string | null) => void;
  clearManualSelection: () => void;
  toggleAuto: () => void;
  setThreshold: (value: number) => void;
  save: () => Promise<void>;
  setNotice: (notice: string | null) => void;
}

function message(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Something went wrong. The details are in the log file.';
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/** One entry attempt at a time, so a doubled mount never starts two runs. */
let entryPromise: Promise<void> | null = null;

async function runEntry(set: SetFn, get: GetFn): Promise<void> {
  set({ error: null, notice: null });

  let status: MatchStatus;
  try {
    status = await getMatchStatus();
  } catch (error) {
    set({ status: 'failed', message: null, error: message(error) });
    return;
  }

  if (status.state === 'idle') {
    // A fresh run discards the previous run's result and decisions.
    set({
      status: 'running',
      runId: null,
      current: status.current,
      total: status.total,
      message: status.message,
      result: null,
      accepted: [],
      rejected: [],
      manual: [],
      manualOldKey: null,
      manualNewKey: null,
      selectedReviewIndex: null,
      savedUnresolved: null,
    });
    try {
      const started = await runMatch();
      set({ runId: started.run_id });
    } catch (error) {
      set({ status: 'failed', error: message(error) });
      return;
    }
    await poll(set, get);
    return;
  }

  if (status.state === 'running') {
    set({
      status: 'running',
      runId: status.run_id,
      current: status.current,
      total: status.total,
      message: status.message,
    });
    await poll(set, get);
    return;
  }

  if (status.state === 'ready') {
    await loadResult(set);
    return;
  }

  set({ status: 'failed', runId: status.run_id, message: status.message });
}

async function poll(set: SetFn, get: GetFn): Promise<void> {
  for (;;) {
    await delay(POLL_INTERVAL_MS);

    let status: MatchStatus;
    try {
      status = await getMatchStatus();
    } catch (error) {
      // The engine may briefly refuse while the run warms up; show why but
      // keep asking. A later poll clears the message again.
      set({ error: message(error) });
      continue;
    }

    set({
      status: status.state,
      runId: status.run_id ?? get().runId,
      current: status.current,
      total: status.total,
      message: status.message,
      error: null,
    });

    if (status.state === 'ready') {
      await loadResult(set);
      return;
    }
    if (status.state === 'failed') return;
    if (status.state === 'idle') {
      set({ error: 'The match run stopped before producing a result.' });
      return;
    }
  }
}

async function loadResult(set: SetFn): Promise<void> {
  try {
    const result = await fetchMatchResult();
    const reviewCount = result.pairs.filter((pair) => pair.needs_review).length;
    set({
      result,
      status: 'ready',
      error: null,
      selectedReviewIndex: reviewCount > 0 ? 0 : null,
      expandedAuto: false,
    });
  } catch (error) {
    // The engine throws if asked before its run is ready; the caller only
    // asks once status says ready, so this is a real failure worth showing.
    set({ status: 'ready', error: message(error) });
  }
}

export const useMatchingStore = create<MatchingState>((set, get) => ({
  status: 'idle',
  runId: null,
  current: 0,
  total: 0,
  message: null,
  result: null,
  error: null,
  notice: null,

  expandedAuto: false,
  selectedReviewIndex: null,
  manualOldKey: null,
  manualNewKey: null,
  threshold: DEFAULT_ACCEPT_THRESHOLD,

  accepted: [],
  rejected: [],
  manual: [],

  savedUnresolved: null,
  saving: false,

  enter: async () => {
    if (entryPromise) return entryPromise;
    entryPromise = runEntry(set, get);
    try {
      await entryPromise;
    } finally {
      entryPromise = null;
    }
  },

  refresh: async () => {
    await loadResult(set);
  },

  retry: async () => {
    if (entryPromise) return entryPromise;
    entryPromise = runEntry(set, get);
    try {
      await entryPromise;
    } finally {
      entryPromise = null;
    }
  },

  accept: (key) =>
    set((state) => ({
      accepted: state.accepted.includes(key) ? state.accepted : [...state.accepted, key],
      rejected: state.rejected.filter((item) => item !== key),
      manual: state.manual.filter((item) => item.old_key !== key),
      savedUnresolved: null,
    })),

  reject: (key) =>
    set((state) => ({
      rejected: state.rejected.includes(key) ? state.rejected : [...state.rejected, key],
      accepted: state.accepted.filter((item) => item !== key),
      manual: state.manual.filter((item) => item.old_key !== key),
      savedUnresolved: null,
    })),

  chooseAlternative: (oldKey, newKey) =>
    set((state) => ({
      manual:
        newKey === null
          ? state.manual.filter((item) => item.old_key !== oldKey)
          : [
              ...state.manual.filter((item) => item.old_key !== oldKey),
              { old_key: oldKey, new_key: newKey },
            ],
      accepted: state.accepted.filter((item) => item !== oldKey),
      rejected: state.rejected.filter((item) => item !== oldKey),
      savedUnresolved: null,
    })),

  pairManually: (oldKey, newKey) =>
    set((state) => {
      const paired = state.manual.some(
        (item) => item.old_key === oldKey && item.new_key === newKey,
      );
      return {
        manual: paired
          ? state.manual.filter(
              (item) => !(item.old_key === oldKey && item.new_key === newKey),
            )
          : [...state.manual, { old_key: oldKey, new_key: newKey }],
        manualOldKey: null,
        manualNewKey: null,
        accepted: state.accepted.filter((item) => item !== oldKey),
        rejected: state.rejected.filter((item) => item !== oldKey),
        savedUnresolved: null,
      };
    }),

  bulkAccept: (threshold) =>
    set((state) => {
      const keys = reviewPairs(state)
        .filter((pair) => pair.confidence >= threshold)
        .filter((pair) => !state.accepted.includes(pair.old.key))
        .map((pair) => pair.old.key);
      const merged = [...new Set([...state.accepted, ...keys])];
      const keySet = new Set(merged);
      return {
        accepted: merged,
        rejected: state.rejected.filter((item) => !keySet.has(item)),
        manual: state.manual.filter((item) => !keySet.has(item.old_key)),
        savedUnresolved: null,
      };
    }),

  moveReviewSelection: (delta) =>
    set((state) => {
      const pairs = reviewPairs(state);
      if (pairs.length === 0) return {};
      const anchor = state.selectedReviewIndex ?? (delta > 0 ? -1 : pairs.length);
      const next = Math.min(pairs.length - 1, Math.max(0, anchor + delta));
      return { selectedReviewIndex: next };
    }),

  selectReview: (index) => set({ selectedReviewIndex: index }),
  setManualOld: (manualOldKey) => set({ manualOldKey }),
  setManualNew: (manualNewKey) => set({ manualNewKey }),
  clearManualSelection: () => set({ manualOldKey: null, manualNewKey: null }),
  toggleAuto: () => set((state) => ({ expandedAuto: !state.expandedAuto })),

  setThreshold: (value) =>
    set({ threshold: Math.min(1, Math.max(0, value)) }),

  save: async () => {
    const state = get();
    set({ saving: true, error: null });
    try {
      const decided = await postMatchDecisions({
        accepted: state.accepted,
        rejected: state.rejected,
        manual: state.manual,
      });

      const countCopy = `${decided.accepted} accepted, ${decided.rejected} rejected and ${
        decided.manual
      } manual match${decided.manual === 1 ? '' : 'es'}`;

      try {
        const finalized = await finalizeMatch();
        set({
          saving: false,
          savedUnresolved: decided.unresolved_review,
          notice: finalized.path
            ? `Review saved — ${countCopy}. Audit file written to ${finalized.path}.`
            : `Review saved — ${countCopy}.`,
        });
      } catch (error) {
        // 422: no output folder chosen yet. The decisions themselves are kept.
        set({ saving: false, error: message(error) });
      }
    } catch (error) {
      set({ saving: false, error: message(error) });
    }
  },

  setNotice: (notice) => set({ notice }),
}));

/** Everything the matcher placed automatically (needs_review is false). */
export function autoPairs(state: MatchingState): MatchPair[] {
  return (state.result?.pairs ?? []).filter((pair) => !pair.needs_review);
}

/** The pairs the user must look at. */
export function reviewPairs(state: MatchingState): MatchPair[] {
  return (state.result?.pairs ?? []).filter((pair) => pair.needs_review);
}

/** The old sheet keys behind a review pair, whichever way it was decided. */
export function decidedKeys(state: MatchingState): Set<string> {
  const keys = new Set(state.accepted);
  for (const key of state.rejected) keys.add(key);
  for (const item of state.manual) keys.add(item.old_key);
  return keys;
}

/** How one pair stands right now, before anything is saved. */
export function decisionFor(state: MatchingState, pair: MatchPair): Decision {
  if (state.accepted.includes(pair.old.key)) return 'accepted';
  if (state.rejected.includes(pair.old.key)) return 'rejected';
  if (state.manual.some((item) => item.old_key === pair.old.key)) return 'manual';
  return null;
}

/** Review pairs still waiting on the user. */
export function unresolvedReviewPairs(state: MatchingState): {
  pairs: MatchPair[];
  count: number;
} {
  const decided = decidedKeys(state);
  const pairs = reviewPairs(state).filter((pair) => !decided.has(pair.old.key));
  return { pairs, count: pairs.length };
}

/** Every count the summary sentences and section headers need. */
export function countByStatus(state: MatchingState): {
  auto: number;
  review: number;
  unresolved: number;
  accepted: number;
  rejected: number;
  manual: number;
  oldUnmatched: number;
  newUnmatched: number;
  unmatched: number;
} {
  const result = state.result;
  const pairs = result?.pairs ?? [];
  const review = reviewPairs(state);
  const decided = decidedKeys(state);
  const manualKeys = new Set(state.manual.map((item) => item.old_key));
  const oldUnmatched = result?.old_unmatched.length ?? 0;
  const newUnmatched = result?.new_unmatched.length ?? 0;
  return {
    auto: pairs.length - review.length,
    review: review.length,
    unresolved: review.filter((pair) => !decided.has(pair.old.key)).length,
    accepted: review.filter((pair) => state.accepted.includes(pair.old.key)).length,
    rejected: review.filter((pair) => state.rejected.includes(pair.old.key)).length,
    manual: review.filter((pair) => manualKeys.has(pair.old.key)).length,
    oldUnmatched,
    newUnmatched,
    unmatched: oldUnmatched + newUnmatched,
  };
}

/**
 * The rename step opens once every review pair is decided and the review has
 * been saved with nothing left unresolved. With nothing to decide or save,
 * the way is open straight away.
 */
export function canContinue(state: MatchingState): boolean {
  if (state.status !== 'ready' || state.result === null || state.saving) return false;
  const nothingToDo =
    unresolvedReviewPairs(state).count === 0 &&
    state.accepted.length + state.rejected.length + state.manual.length === 0;
  return state.savedUnresolved === 0 || nothingToDo;
}

/** Review pairs above a threshold that a bulk accept would newly decide. */
export function candidatesForThreshold(state: MatchingState, threshold: number): MatchPair[] {
  return reviewPairs(state).filter(
    (pair) =>
      pair.confidence >= threshold && decisionFor(state, pair) !== 'accepted',
  );
}
