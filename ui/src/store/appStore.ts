/**
 * Application-level state: which screen is showing, and whether the engine
 * is reachable.
 *
 * Screen state lives in its own store. This one holds only what every screen
 * needs to know.
 *
 * Navigation is a graph, not a wizard. Comparing drawings and renaming them
 * are two separate jobs a user comes here to do, and either can be done
 * without the other; the screens between them (register, matching,
 * alignment) are stages you may look at, not gates you must pass. So every
 * screen is reachable from the top bar whenever the work it needs exists,
 * and each tool arranges its own prerequisites on the engine.
 */

import { create } from 'zustand';

import { ApiError, fetchHealth } from '../api/client';
import type { HealthResponse } from '../api/types';
import { isDesktop } from '../lib/native';

export type ConnectionStatus = 'idle' | 'connecting' | 'ready' | 'error';

export type ScreenName =
  | 'home'
  | 'setup'
  | 'register'
  | 'matching'
  | 'rename'
  | 'alignment'
  | 'manual'
  | 'changes'
  | 'viewer';

/** Which of the two tools the user picked, so screens can offer the right way back. */
export type ToolName = 'compare' | 'rename' | null;

interface AppState {
  status: ConnectionStatus;
  health: HealthResponse | null;
  errorMessage: string | null;
  /** True inside the desktop shell, false in a plain browser tab. */
  desktop: boolean;
  screen: ScreenName;
  /** The job in progress, which decides where "continue" leads. */
  tool: ToolName;
  /** The pair the viewer opens on: an index into the align/compare results. */
  viewerPair: number | null;

  connect: () => Promise<void>;
  /** Go to any screen. Screens guard their own prerequisites. */
  go: (screen: ScreenName) => void;
  /** Pick a tool and go to its first screen. */
  startTool: (tool: Exclude<ToolName, null>) => void;
  /** Open the full-size viewer on one pair. */
  openViewer: (pairIndex: number) => void;

  goToHome: () => void;
  goToRegister: () => void;
  goToSetup: () => void;
  goToMatching: () => void;
  goToRename: () => void;
  goToAlignment: () => void;
  goToManual: () => void;
  goToChanges: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  status: 'idle',
  health: null,
  errorMessage: null,
  desktop: false,
  screen: 'home',
  tool: null,
  viewerPair: null,

  connect: async () => {
    set({ status: 'connecting', errorMessage: null });
    try {
      const health = await fetchHealth();
      set({ status: 'ready', health, errorMessage: null, desktop: isDesktop() });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : 'The engine could not be reached. Start it and try again.';
      set({ status: 'error', health: null, errorMessage: message, desktop: isDesktop() });
    }
  },

  go: (screen) => set({ screen }),

  startTool: (tool) => set({ tool, screen: 'setup' }),

  openViewer: (pairIndex) => set({ screen: 'viewer', viewerPair: pairIndex }),

  goToHome: () => set({ screen: 'home' }),
  goToRegister: () => set({ screen: 'register' }),
  goToSetup: () => set({ screen: 'setup' }),
  goToMatching: () => set({ screen: 'matching' }),
  goToRename: () => set({ screen: 'rename' }),
  goToAlignment: () => set({ screen: 'alignment' }),
  goToManual: () => set({ screen: 'manual' }),
  goToChanges: () => set({ screen: 'changes' }),
}));
