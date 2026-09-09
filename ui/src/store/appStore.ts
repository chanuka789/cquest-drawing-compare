/**
 * Application-level state: which screen is showing, and whether the engine
 * is reachable.
 *
 * Screen state lives in its own store. This one holds only what every screen
 * needs to know.
 */

import { create } from 'zustand';

import { ApiError, fetchHealth } from '../api/client';
import type { HealthResponse } from '../api/types';
import { isDesktop } from '../lib/native';

export type ConnectionStatus = 'idle' | 'connecting' | 'ready' | 'error';
export type ScreenName = 'setup' | 'register' | 'matching' | 'rename' | 'alignment' | 'manual';

interface AppState {
  status: ConnectionStatus;
  health: HealthResponse | null;
  errorMessage: string | null;
  /** True inside the desktop shell, false in a plain browser tab. */
  desktop: boolean;
  screen: ScreenName;

  connect: () => Promise<void>;
  goToRegister: () => void;
  goToSetup: () => void;
  goToMatching: () => void;
  goToRename: () => void;
  goToAlignment: () => void;
  goToManual: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  status: 'idle',
  health: null,
  errorMessage: null,
  desktop: false,
  screen: 'setup',

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

  goToRegister: () => set({ screen: 'register' }),
  goToSetup: () => set({ screen: 'setup' }),
  goToMatching: () => set({ screen: 'matching' }),
  goToRename: () => set({ screen: 'rename' }),
  goToAlignment: () => set({ screen: 'alignment' }),
  goToManual: () => set({ screen: 'manual' }),
}));
