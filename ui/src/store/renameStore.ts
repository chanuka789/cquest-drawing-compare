/**
 * State for the Rename review screen.
 *
 * The plan lives on the engine: every change to the template, mode, folder
 * switch or inline override re-posts `/api/rename/plan` (debounced by the
 * screen) and the row table shows whatever dry run comes back. Nothing
 * touches the disk until "Apply renames", and applying always re-posts the
 * plan first so inline edits and collision re-checks are honoured at apply
 * time.
 *
 * Apply and undo run on the engine and are POLLED here every ~700ms — there
 * is no progress socket for rename. A generation counter makes sure a poll
 * that returns after the screen unmounted (or after a newer run started)
 * never overwrites newer state.
 */

import { create } from 'zustand';

import {
  ApiError,
  applyRename,
  cancelRename,
  fetchRenameContext,
  fetchRenameStatus,
  fetchRenameTemplates,
  postRenamePlan,
  undoRename,
} from '../api/client';
import type {
  RenameAction,
  RenameContext,
  RenameFailedItem,
  RenameMode,
  RenamePlan,
  RenameStatus,
  RenameTemplate,
} from '../api/types';

/** How often the screen asks where the apply/undo run has got to. */
const POLL_INTERVAL_MS = 700;

/** The preset the template input starts on, when the engine ships it. */
const DEFAULT_PRESET_ID = 'iso19650';

type SetFn = (
  partial:
    | Partial<RenameState>
    | ((state: RenameState) => Partial<RenameState>),
) => void;
type GetFn = () => RenameState;

/** Where the screen is in an apply/undo run. */
export type RenamePhase = 'idle' | 'running' | 'done' | 'failed';
export type RunKind = 'rename' | 'undo';

/** Which rows the plan table shows. */
export type RenameFilter = 'all' | 'ok' | 'problems' | 'unchanged';

export interface RenameProgress {
  current: number;
  total: number;
  current_item: string | null;
}

interface RenameState {
  /** True once templates/context have been fetched at least once. */
  ready: boolean;

  templates: RenameTemplate[];
  context: RenameContext;
  /** Chosen preset id, or null once the template text has been edited. */
  presetId: string | null;
  /** The raw template text being edited. */
  template: string;
  mode: RenameMode;
  useDisciplineFolders: boolean;
  /** source_path -> replacement file name chosen in the review table. */
  overrides: Record<string, string>;

  /** The last successful plan, or null before the first dry run. */
  plan: RenamePlan | null;
  /** True between a debounced request firing and its plan arriving. */
  planning: boolean;
  /** A plan request failed, e.g. "no drawings to rename" (verbatim). */
  planError: string | null;
  /** Fingerprint of the inputs the current plan was built from. */
  planned: string | null;

  /** Which rows the table shows. */
  filter: RenameFilter;

  /** Lifecycle of an apply/undo run (idle = back on the plan). */
  phase: RenamePhase;
  kind: RunKind | null;
  runId: string | null;
  progress: RenameProgress;
  /** Engine progress copy while running; outcome message once settled. */
  message: string | null;
  /** Engine error, shown verbatim. */
  error: string | null;
  completed: number;
  failed: number;
  failedItems: RenameFailedItem[];
  undoLogPath: string | null;
  /** Whether the result panel may offer "Undo all renames". */
  undoEnabled: boolean;
  /** Transient outcome lines, e.g. after an undo drops back on the plan. */
  notice: string | null;

  init: () => Promise<void>;
  setPreset: (presetId: string) => void;
  setTemplate: (template: string) => void;
  setMode: (mode: RenameMode) => void;
  setFolders: (useDisciplineFolders: boolean) => void;
  setOverride: (source: string, newName: string) => void;
  setFilter: (filter: RenameFilter) => void;
  /** Re-post the plan with the current template/mode/folders/overrides. */
  planNow: () => Promise<void>;
  /** Re-post the plan after a transient failure. */
  refreshPlan: () => Promise<void>;
  /** Re-post the plan, then apply it. The dialog confirms i_understand. */
  apply: (iUnderstand: boolean) => Promise<void>;
  cancel: () => Promise<void>;
  undo: () => Promise<void>;
  /** Leave the result panel and show the review table again. */
  backToPlan: () => void;
  /** Stop the poller when the screen unmounts; the engine run continues. */
  leave: () => void;
  setNotice: (notice: string | null) => void;
}

function message(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Something went wrong. The details are in the log file.';
}

/** The request body every plan post is built from. */
function planBody(state: RenameState) {
  return {
    template: state.template,
    mode: state.mode,
    use_discipline_folders: state.useDisciplineFolders,
    overrides: state.overrides,
  };
}

/** A stable tag for the inputs a plan was rendered from. */
function planFingerprint(state: RenameState): string {
  return JSON.stringify(planBody(state));
}

// ── The plan loop: single-flight, latest request wins ──────────────────

/** Bumped on every planNow request; the runner loops until it stops moving. */
let planVersion = 0;
let planRunner: Promise<void> | null = null;

async function runPlanLoop(set: SetFn, get: GetFn): Promise<void> {
  for (;;) {
    const seen = planVersion;
    const state = get();
    if (!state.template.trim()) {
      // Nothing to render. If a newer request arrived while we looked, go
      // around again; otherwise the plan simply stays as it was.
      if (planVersion === seen) {
        set({ planning: false, planError: null });
        return;
      }
      continue;
    }

    try {
      const plan = await postRenamePlan(planBody(state));
      if (planVersion === seen) {
        set({
          plan,
          planned: planFingerprint(state),
          planning: false,
          planError: null,
          error: null,
        });
        return;
      }
    } catch (error) {
      if (planVersion === seen) {
        set({ planning: false, planError: message(error) });
        return;
      }
    }
    // A newer request queued while that one flew: loop and send the freshest
    // plan. Typing never stacks requests — one flight at a time.
  }
}

function schedulePlanNow(set: SetFn, get: GetFn): Promise<void> {
  planVersion += 1;
  set({ planning: true, planError: null });
  if (planRunner === null) {
    planRunner = runPlanLoop(set, get).finally(() => {
      planRunner = null;
    });
  }
  return planRunner;
}

// ── The run poller: one interval while a run is going ──────────────────

let pollGeneration = 0;
let pollTimer: number | null = null;

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** The user navigated away or a newer run started: stale polls are dropped. */
function invalidatePolls(): void {
  pollGeneration += 1;
  stopPolling();
}

function startPolling(): void {
  invalidatePolls();
  const generation = pollGeneration;
  pollTimer = window.setInterval(() => {
    void pollTick(generation);
  }, POLL_INTERVAL_MS);
}

async function pollTick(generation: number): Promise<void> {
  if (pollGeneration !== generation) return;
  let status: RenameStatus;
  try {
    status = await fetchRenameStatus();
  } catch (error) {
    if (pollGeneration === generation) setErrorFromPoll(error);
    return;
  }
  if (pollGeneration !== generation) return;

  if (status.state === 'running') {
    setRunning(status);
    return;
  }
  stopPolling();
  settleRun(status);
}

/** Read/write the live store; kept module-level so the poller needs no args. */
function readState(): RenameState {
  return useRenameStore.getState();
}

function writeState(partial: Partial<RenameState> | ((state: RenameState) => Partial<RenameState>)): void {
  useRenameStore.setState(partial);
}

function setErrorFromPoll(error: unknown): void {
  writeState({ error: message(error) });
}

function setRunning(status: RenameStatus): void {
  writeState({
    message: status.message,
    error: null,
    progress: {
      current: status.current,
      total: status.total,
      current_item: status.current_item,
    },
    completed: status.completed,
    failed: status.failed,
  });
}

/** A poll reached a terminal engine state: map it onto the screen. */
function settleRun(status: RenameStatus): void {
  const state = readState();
  const kind = state.kind;
  const common = {
    progress: {
      current: status.current,
      total: status.total,
      current_item: status.current_item,
    },
    message: status.message,
    completed: status.completed,
    failed: status.failed,
    failedItems: status.failed_items,
    undoLogPath: status.undo_log_path,
    runId: status.run_id ?? state.runId,
  };

  if (status.state === 'failed') {
    writeState({
      ...common,
      phase: 'failed',
      error: status.error ?? status.message ?? 'The rename run failed.',
    });
    return;
  }

  if (kind === 'undo') {
    // Reversal is over: drop back onto the plan with the outcome shown.
    const reasons = status.failed_items
      .map((item) => item.reason ?? item.error)
      .filter((text): text is string => typeof text === 'string' && text.length > 0);
    writeState({
      ...common,
      phase: 'idle',
      kind: null,
      runId: null,
      undoEnabled: false,
      notice:
        status.message ??
        (status.state === 'cancelled' ? 'Undo stopped.' : 'Undo finished.'),
      error: reasons.length > 0 ? reasons.join(' ') : null,
    });
    return;
  }

  // kind === 'rename': the result panel appears, undo offered.
  writeState({
    ...common,
    phase: 'done',
    error: null,
    undoEnabled: status.completed > 0,
  });
}

function beginRun(kind: RunKind, runId: string): void {
  writeState({
    phase: 'running',
    kind,
    runId,
    progress: { current: 0, total: 0, current_item: null },
    message: null,
    error: null,
    completed: 0,
    failed: 0,
    failedItems: [],
    undoLogPath: null,
    undoEnabled: false,
    notice: null,
  });
  startPolling();
}

// ── Init ───────────────────────────────────────────────────────────────

let entryPromise: Promise<void> | null = null;

async function runInit(): Promise<void> {
  const set = writeState;
  const get = readState;
  set({ error: null, notice: null });

  try {
    const [templates, context] = await Promise.all([
      fetchRenameTemplates(),
      fetchRenameContext(),
    ]);
    set((state) => {
      // A returning visit keeps the user's template and overrides; only the
      // very first load adopts the default preset.
      if (!state.ready) {
        const preset =
          templates.find((item) => item.id === DEFAULT_PRESET_ID) ??
          (templates[0] ?? null);
        return {
          ready: true,
          templates,
          context,
          presetId: preset?.id ?? null,
          template: preset ? preset.template : state.template,
        };
      }
      return { ready: true, templates, context };
    });
  } catch (error) {
    set({ ready: true, error: message(error) });
  }

  // Reconcile a run left going while the screen was unmounted: adopt its
  // terminal state, or resume polling it. An idle engine while we think a run
  // finished or failed means the session/workspace was reset while we were
  // away, so the result view would be stale — drop back to the plan.
  try {
    const status = await fetchRenameStatus();
    const phase = get().phase;
    if (phase === 'running' && status.state !== 'running') {
      if (status.state === 'idle') {
        // The engine session was reset while we were away.
        writeState({
          phase: 'failed',
          error: 'The rename run was interrupted before it finished.',
        });
      } else {
        settleRun(status);
      }
    } else if (phase === 'running' && status.state === 'running') {
      const kind = status.kind ?? 'rename';
      beginRun(kind, status.run_id ?? '');
    } else if (phase !== 'idle' && status.state === 'idle') {
      writeState({
        phase: 'idle',
        kind: null,
        runId: null,
        progress: { current: 0, total: 0, current_item: null },
        message: null,
        error: null,
        completed: 0,
        failed: 0,
        failedItems: [],
        undoLogPath: null,
        undoEnabled: false,
        notice: null,
      });
    }
  } catch {
    // Offline: the plan screen already shows why. The run, if any, will
    // surface through the poller once the engine answers again.
  }
}

// ── The store ──────────────────────────────────────────────────────────

export const useRenameStore = create<RenameState>((set, get) => ({
  ready: false,

  templates: [],
  context: {},
  presetId: null,
  template: '',
  mode: 'copy',
  useDisciplineFolders: false,
  overrides: {},

  plan: null,
  planning: false,
  planError: null,
  planned: null,

  filter: 'all',

  phase: 'idle',
  kind: null,
  runId: null,
  progress: { current: 0, total: 0, current_item: null },
  message: null,
  error: null,
  completed: 0,
  failed: 0,
  failedItems: [],
  undoLogPath: null,
  undoEnabled: false,
  notice: null,

  init: async () => {
    if (entryPromise) return entryPromise;
    entryPromise = runInit();
    try {
      await entryPromise;
    } finally {
      entryPromise = null;
    }
  },

  setPreset: (presetId) =>
    set((state) => {
      const preset = state.templates.find((item) => item.id === presetId);
      if (!preset) return {};
      return { presetId: preset.id, template: preset.template, planError: null };
    }),

  setTemplate: (template) =>
    set((state) => {
      // Typing any text that is not exactly the preset's marks it custom.
      const preset = state.templates.find((item) => item.id === state.presetId);
      const presetId = preset && template === preset.template ? preset.id : null;
      return { template, presetId, planError: null };
    }),

  setMode: (mode) => set({ mode }),
  setFolders: (useDisciplineFolders) => set({ useDisciplineFolders }),

  setOverride: (source, newName) =>
    set((state) => {
      const overrides = { ...state.overrides };
      const trimmed = newName.trim();
      if (trimmed.length === 0) delete overrides[source];
      else overrides[source] = trimmed;
      return { overrides };
    }),

  setFilter: (filter) => set({ filter }),

  planNow: () => schedulePlanNow(set, get),
  refreshPlan: () => schedulePlanNow(set, get),

  apply: async (iUnderstand) => {
    // Always re-post the plan with the CURRENT template, mode, folders and
    // overrides immediately before applying, so inline edits and collision
    // re-checks are honoured at apply time.
    try {
      const body = planBody(get());
      const plan = await postRenamePlan(body);
      set({ plan, planned: JSON.stringify(body) });
      if (!plan.can_apply || plan.errors.length > 0) {
        set({
          planError:
            plan.errors.length > 0
              ? plan.errors.join(' ')
              : 'The plan cannot be applied until its problems are fixed.',
        });
        return;
      }
      const started = await applyRename(iUnderstand);
      beginRun('rename', started.run_id);
    } catch (error) {
      set({ error: message(error) });
    }
  },

  cancel: async () => {
    try {
      await cancelRename();
    } catch (error) {
      set({ error: message(error) });
    }
  },

  undo: async () => {
    set({ error: null, notice: null });
    try {
      const started = await undoRename();
      beginRun('undo', started.run_id);
    } catch (error) {
      set({ error: message(error) });
    }
  },

  backToPlan: () =>
    set({
      phase: 'idle',
      kind: null,
      runId: null,
      undoEnabled: false,
      message: null,
      error: null,
      notice: null,
    }),

  leave: () => invalidatePolls(),

  setNotice: (notice) => set({ notice }),
}));

// ── Pure helpers ────────────────────────────────────────────────────────

/** Statuses that are fine as they are: no human attention needed. */
function isClean(status: RenameAction['status']): boolean {
  return status === 'ok' || status === 'unchanged';
}

export function isProblemAction(action: RenameAction): boolean {
  return !isClean(action.status);
}

/** The problem rows — anything not ok or unchanged — in plan order. */
export function problems(state: RenameState): RenameAction[] {
  return (state.plan?.actions ?? []).filter(isProblemAction);
}

/** Every row with problem rows on top, the reason to open the table. */
export function orderedActions(state: RenameState): RenameAction[] {
  const actions = state.plan?.actions ?? [];
  return [...actions.filter(isProblemAction), ...actions.filter((a) => !isProblemAction(a))];
}

/** The rows the current filter shows, problems already sorted to the top. */
export function visibleActions(state: RenameState): RenameAction[] {
  const all = orderedActions(state);
  switch (state.filter) {
    case 'ok':
      return all.filter((action) => action.status === 'ok');
    case 'unchanged':
      return all.filter((action) => action.status === 'unchanged');
    case 'problems':
      return all.filter(isProblemAction);
    default:
      return all;
  }
}

/** Counts for the filter chips and the bottom summary bar. */
export function renameCounts(state: RenameState): {
  toChange: number;
  unchanged: number;
  attention: number;
  total: number;
} {
  const summary = state.plan?.summary;
  return {
    toChange: summary?.to_change ?? 0,
    unchanged: summary?.unchanged ?? 0,
    attention: summary?.problems ?? 0,
    total: state.plan?.actions.length ?? 0,
  };
}

/** "N will be renamed · M unchanged · K need attention", sentence case. */
export function summarySentence(state: RenameState): string {
  const counts = renameCounts(state);
  const parts: string[] = [];
  if (counts.total > 0) {
    parts.push(`${counts.toChange} will be renamed`);
    parts.push(`${counts.unchanged} unchanged`);
    parts.push(`${counts.attention} ${counts.attention === 1 ? 'needs' : 'need'} attention`);
    return parts.join(' · ');
  }
  return 'Build a plan to rename the current issue.';
}

/** True when "Apply renames" may run: a clean plan that matches the inputs. */
export function canApply(state: RenameState): boolean {
  return applyBlockReason(state) === null;
}

/** Why the apply button is disabled, or null when it may be pressed. */
export function applyBlockReason(state: RenameState): string | null {
  if (!state.template.trim()) return 'Type a template to build a plan.';
  if (state.planned !== planFingerprint(state)) return 'Updating the plan…';
  if (!state.plan) return 'Waiting for the plan…';
  const plan = state.plan;
  if (plan.errors.length > 0) return plan.errors.join(' ');
  if (!plan.can_apply) return 'The plan has problems that need fixing first.';
  if (plan.summary.to_change === 0) {
    return 'Nothing to rename — the names already match the template.';
  }
  return null;
}
