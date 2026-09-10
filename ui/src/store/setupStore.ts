/**
 * State for the Setup screen: the three folders, the drawing list, and the
 * run options.
 *
 * The fast pass runs the moment a folder is chosen, so rows appear at once;
 * the deep pass then starts in the background and the rows fill in.
 */

import { create } from 'zustand';

import {
  ApiError,
  cancelScan,
  clearDrawingList,
  fetchOutput,
  fetchOutputSuggestion,
  fetchProfiles,
  fetchSheets,
  fetchSides,
  previewDrawingList,
  setFolder as apiSetFolder,
  setOptions,
  setOutput,
  startScan,
  validateOutput,
} from '../api/client';
import type {
  IssueSide,
  ListParseResult,
  OutputValidation,
  SheetProfileOption,
  SheetRow,
  SideState,
} from '../api/types';
import { NativeError, pickFile, pickFolder } from '../lib/native';

/** Tolerance is always in millimetres at drawing scale, never in pixels. */
export const TOLERANCES = [
  { id: 'tight', label: '10 mm', millimetres: 10 },
  { id: 'standard', label: '25 mm', millimetres: 25 },
  { id: 'loose', label: '50 mm', millimetres: 50 },
] as const;

const DRAWING_LIST_TYPES = ['Drawing list (*.xlsx;*.xls;*.csv;*.pdf)', 'All files (*.*)'];

const DIALOG_TITLES: Record<IssueSide, string> = {
  old: 'Choose the folder for the previous issue',
  new: 'Choose the folder for the current issue',
};

const EMPTY_SIDE: SideState = {
  side: 'old',
  folder: null,
  headline: 'Not scanned yet',
  file_count: 0,
  sheet_count: 0,
  identified_count: 0,
  attention_count: 0,
  is_scanning: false,
  error: null,
  other_files: {},
  skipped_count: 0,
};

interface SetupState {
  old: SideState;
  new: SideState;
  sheets: Record<IssueSide, SheetRow[]>;
  expanded: Record<IssueSide, boolean>;
  filter: Record<IssueSide, string>;
  sort: Record<IssueSide, SheetSortKey>;
  sortAscending: Record<IssueSide, boolean>;

  outputFolder: string | null;
  outputSuggestion: string | null;
  outputValidation: OutputValidation | null;

  drawingListPath: string | null;
  listParse: ListParseResult | null;

  profiles: SheetProfileOption[];
  profileId: string;
  toleranceId: string;

  notice: string | null;
  busy: boolean;
  /** Increments when both folders are complete, to fire the seam pulse. */
  pulseToken: number;

  init: () => Promise<void>;
  chooseFolder: (side: IssueSide) => Promise<void>;
  refreshSide: (side: IssueSide) => Promise<void>;
  refreshSheets: (side: IssueSide) => Promise<void>;
  refreshAll: () => Promise<void>;
  cancel: () => Promise<void>;
  toggleExpanded: (side: IssueSide) => void;
  setFilter: (side: IssueSide, text: string) => void;
  setSort: (side: IssueSide, key: SheetSortKey) => void;

  chooseOutput: () => Promise<void>;
  acceptSuggestedOutput: () => Promise<void>;

  chooseDrawingList: () => Promise<void>;
  removeDrawingList: () => Promise<void>;

  setProfile: (id: string) => Promise<void>;
  setTolerance: (id: string) => Promise<void>;
  setNotice: (message: string | null) => void;
}

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  // A NativeError already carries wording written for the user.
  if (error instanceof NativeError) return error.message;
  return 'Something went wrong. The details are in the log file.';
}

export const useSetupStore = create<SetupState>((set, get) => ({
  old: { ...EMPTY_SIDE, side: 'old' },
  new: { ...EMPTY_SIDE, side: 'new' },
  sheets: { old: [], new: [] },
  expanded: { old: false, new: false },
  filter: { old: '', new: '' },
  sort: { old: 'drawing_no', new: 'drawing_no' },
  sortAscending: { old: true, new: true },

  outputFolder: null,
  outputSuggestion: null,
  outputValidation: null,

  drawingListPath: null,
  listParse: null,

  profiles: [{ id: 'default', label: 'Default' }],
  profileId: 'default',
  toleranceId: 'standard',

  notice: null,
  busy: false,
  pulseToken: 0,

  init: async () => {
    try {
      // The output folder is read back from the engine, not assumed. The
      // engine may already hold a workspace (it creates one on demand for
      // the viewer), and a screen that offered "Choose a folder" while the
      // engine had one was the two disagreeing in public.
      const [profiles, sides, output] = await Promise.all([
        fetchProfiles(),
        fetchSides(),
        fetchOutput().catch(() => null),
      ]);
      const byside = Object.fromEntries(sides.map((item) => [item.side, item])) as Record<
        IssueSide,
        SideState
      >;
      set({
        profiles,
        old: byside.old ?? get().old,
        new: byside.new ?? get().new,
        outputFolder: output?.folder ?? get().outputFolder,
      });
    } catch {
      // The engine may still be starting. The health check reports that.
    }
  },

  chooseFolder: async (side) => {
    set({ notice: null });
    let chosen: string | null;
    try {
      chosen = await pickFolder(DIALOG_TITLES[side]);
    } catch (error) {
      set({ notice: message(error) });
      return;
    }
    if (!chosen) return;

    set({ busy: true });
    try {
      const state = await apiSetFolder(side, chosen);
      const before = get();
      const bothBefore = Boolean(before.old.folder && before.new.folder);
      const bothNow = Boolean(
        (side === 'old' ? chosen : before.old.folder) &&
          (side === 'new' ? chosen : before.new.folder),
      );

      set({
        [side]: state,
        expanded: { ...before.expanded, [side]: true },
        // The seam pulses only on the move from one folder to two.
        pulseToken: bothNow && !bothBefore ? before.pulseToken + 1 : before.pulseToken,
      } as Partial<SetupState>);

      await get().refreshSheets(side);
      await startScan(side);
      void get().refreshAll();

      if (bothNow) void get().acceptSuggestedOutput();
    } catch (error) {
      set({ notice: message(error) });
    } finally {
      set({ busy: false });
    }
  },

  refreshSide: async (side) => {
    try {
      const sides = await fetchSides();
      const found = sides.find((item) => item.side === side);
      if (found) set({ [side]: found } as Partial<SetupState>);
    } catch {
      /* transient; the next tick will pick it up */
    }
  },

  refreshSheets: async (side) => {
    try {
      const rows = await fetchSheets(side);
      set({ sheets: { ...get().sheets, [side]: rows } });
    } catch {
      /* transient */
    }
  },

  refreshAll: async () => {
    await Promise.all([
      get().refreshSide('old'),
      get().refreshSide('new'),
      get().refreshSheets('old'),
      get().refreshSheets('new'),
    ]);
  },

  cancel: async () => {
    try {
      await cancelScan();
      set({ notice: 'Scan stopped. The drawings already read are kept.' });
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  toggleExpanded: (side) =>
    set({ expanded: { ...get().expanded, [side]: !get().expanded[side] } }),

  setFilter: (side, text) => set({ filter: { ...get().filter, [side]: text } }),

  setSort: (side, key) => {
    const state = get();
    if (state.sort[side] === key) {
      set({ sortAscending: { ...state.sortAscending, [side]: !state.sortAscending[side] } });
      return;
    }
    set({
      sort: { ...state.sort, [side]: key },
      sortAscending: { ...state.sortAscending, [side]: true },
    });
  },

  // ── Output folder ────────────────────────────────────────────────────

  acceptSuggestedOutput: async () => {
    try {
      const { folder } = await fetchOutputSuggestion();
      if (!folder) return;
      set({ outputSuggestion: folder });

      if (!get().outputFolder) {
        const validation = await validateOutput(folder);
        set({ outputValidation: validation });
        if (validation.is_valid) {
          await setOutput(folder);
          set({ outputFolder: folder });
        }
      }
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  chooseOutput: async () => {
    let chosen: string | null;
    try {
      chosen = await pickFolder('Choose where to save the results');
    } catch (error) {
      set({ notice: message(error) });
      return;
    }
    if (!chosen) return;

    try {
      const validation = await validateOutput(chosen);
      set({ outputValidation: validation });

      if (!validation.is_valid) {
        set({ outputFolder: null, notice: validation.errors[0] ?? null });
        return;
      }

      await setOutput(chosen);
      set({ outputFolder: chosen, notice: null });
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  // ── The drawing list ─────────────────────────────────────────────────

  chooseDrawingList: async () => {
    let chosen: string | null;
    try {
      chosen = await pickFile('Choose the issued drawing list', DRAWING_LIST_TYPES);
    } catch (error) {
      set({ notice: message(error) });
      return;
    }
    if (!chosen) return;

    set({ busy: true, notice: null });
    try {
      const parse = await previewDrawingList(chosen);
      set({ drawingListPath: chosen, listParse: parse, notice: null });
    } catch (error) {
      set({ notice: message(error) });
    } finally {
      set({ busy: false });
    }
  },

  removeDrawingList: async () => {
    try {
      await clearDrawingList();
    } catch {
      /* clearing is best-effort */
    }
    set({ drawingListPath: null, listParse: null });
  },

  // ── Options ──────────────────────────────────────────────────────────

  setProfile: async (id) => {
    set({ profileId: id });
    try {
      await setOptions({ profile_id: id });
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  setTolerance: async (id) => {
    const option = TOLERANCES.find((item) => item.id === id);
    set({ toleranceId: id });
    if (!option) return;
    try {
      await setOptions({ tolerance_mm: option.millimetres });
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  setNotice: (notice) => set({ notice }),
}));

/** Both folders chosen and the scans finished: the register can be built. */
export function canBuildRegister(state: SetupState): boolean {
  return (
    Boolean(state.old.folder && state.new.folder) &&
    !state.old.is_scanning &&
    !state.new.is_scanning &&
    state.old.sheet_count + state.new.sheet_count > 0
  );
}

/** What the drawing list in a folder panel can be ordered by. */
export type SheetSortKey = 'drawing_no' | 'title' | 'revision';

/** Rows for one panel, after the filter box and the chosen sort. */
export function visibleSheets(state: SetupState, side: IssueSide): SheetRow[] {
  const text = state.filter[side].trim().toLowerCase();
  let rows = state.sheets[side];

  if (text) {
    rows = rows.filter((row) =>
      [row.drawing_no, row.title, row.filename, row.revision]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(text)),
    );
  }

  const key = state.sort[side];
  const direction = state.sortAscending[side] ? 1 : -1;

  return [...rows].sort((left, right) => {
    // A sheet with no number sorts by its file name, so unidentified rows
    // stay findable rather than clumping under an empty value.
    const a = String(left[key] ?? (key === 'drawing_no' ? left.filename : ''));
    const b = String(right[key] ?? (key === 'drawing_no' ? right.filename : ''));
    return a.localeCompare(b, undefined, { numeric: true }) * direction;
  });
}
