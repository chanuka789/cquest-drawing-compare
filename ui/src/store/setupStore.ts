/**
 * State for the Setup screen: the two issue folders, the optional drawing
 * list, and the run options.
 *
 * Phase 1 stops here. Building the register is Phase 2.
 */

import { create } from 'zustand';

import { NO_BRIDGE_MESSAGE, isDesktop, pickFile, pickFolder } from '../lib/native';
import { isSameFolder } from '../lib/path';
import type { SheetProfileOption, ToleranceOption } from '../api/types';

export type IssueSide = 'previous' | 'current';

/** Phase 2 loads these from `profiles/`. Hard-coded until then. */
export const SHEET_PROFILES: SheetProfileOption[] = [
  { id: 'default', label: 'Default' },
  { id: 'keo-a1', label: 'KEO A1' },
];

/** Tolerance is always in millimetres at drawing scale, never in pixels. */
export const TOLERANCES: ToleranceOption[] = [
  { id: 'tight', label: '10 mm', millimetres: 10 },
  { id: 'standard', label: '25 mm', millimetres: 25 },
  { id: 'loose', label: '50 mm', millimetres: 50 },
];

const DRAWING_LIST_TYPES = [
  'Drawing list (*.xlsx;*.xls;*.csv;*.pdf;*.docx)',
  'All files (*.*)',
];

interface SetupState {
  previousFolder: string | null;
  currentFolder: string | null;
  drawingList: string | null;
  profileId: string;
  toleranceId: string;
  /** Shown under the panels. Always phrased as what to do next. */
  notice: string | null;
  /** Increments each time both folders are complete, to fire the seam pulse. */
  pulseToken: number;

  chooseFolder: (side: IssueSide) => Promise<void>;
  setFolder: (side: IssueSide, path: string) => void;
  chooseDrawingList: () => Promise<void>;
  clearDrawingList: () => void;
  setProfile: (id: string) => void;
  setTolerance: (id: string) => void;
  setNotice: (message: string | null) => void;
}

const DIALOG_TITLES: Record<IssueSide, string> = {
  previous: 'Choose the folder for the previous issue',
  current: 'Choose the folder for the current issue',
};

export const useSetupStore = create<SetupState>((set, get) => ({
  previousFolder: null,
  currentFolder: null,
  drawingList: null,
  profileId: 'default',
  toleranceId: 'standard',
  notice: null,
  pulseToken: 0,

  setFolder: (side, path) => {
    const state = get();
    const other = side === 'previous' ? state.currentFolder : state.previousFolder;

    if (isSameFolder(path, other)) {
      set({
        notice:
          'Both issues point at the same folder. Choose a different folder for one of them.',
      });
      return;
    }

    const next =
      side === 'previous' ? { previousFolder: path } : { currentFolder: path };
    const bothChosenBefore = Boolean(state.previousFolder && state.currentFolder);
    const bothChosenNow = Boolean(
      (side === 'previous' ? path : state.previousFolder) &&
        (side === 'current' ? path : state.currentFolder),
    );

    set({
      ...next,
      notice: null,
      // The seam pulses only on the move from one folder to two.
      pulseToken:
        bothChosenNow && !bothChosenBefore ? state.pulseToken + 1 : state.pulseToken,
    });
  },

  chooseFolder: async (side) => {
    if (!isDesktop()) {
      set({ notice: NO_BRIDGE_MESSAGE });
      return;
    }
    const chosen = await pickFolder(DIALOG_TITLES[side]);
    if (chosen) get().setFolder(side, chosen);
  },

  chooseDrawingList: async () => {
    if (!isDesktop()) {
      set({ notice: NO_BRIDGE_MESSAGE });
      return;
    }
    const chosen = await pickFile('Choose the issued drawing list', DRAWING_LIST_TYPES);
    if (chosen) set({ drawingList: chosen, notice: null });
  },

  clearDrawingList: () => set({ drawingList: null }),
  setProfile: (id) => set({ profileId: id }),
  setTolerance: (id) => set({ toleranceId: id }),
  setNotice: (message) => set({ notice: message }),
}));

/** Both folders chosen: the register can be built. */
export function canBuildRegister(state: SetupState): boolean {
  return Boolean(state.previousFolder && state.currentFolder);
}
