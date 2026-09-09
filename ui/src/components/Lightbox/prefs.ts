/**
 * The lightbox's one persisted preference (Phase 4, Task 4.12).
 *
 * Reviewers develop a view-mode preference and switching back every time
 * is irritating, so the last chosen mode is remembered under its own key.
 * Everything here is deliberately tiny and isolated: one key, four valid
 * values, both accessors guarded against a locked-down or absent
 * `localStorage` (private windows, odd WebView2 builds) — persistence is a
 * convenience, never a requirement.
 */

import type { LightboxMode } from './Lightbox';

/** The only key this module ever touches. */
export const LIGHTBOX_MODE_STORAGE_KEY = 'cqdc.lightbox.mode';

const VALID_MODES: ReadonlyArray<LightboxMode> = ['overlay', 'swipe', 'blink', 'single'];

/** The stored mode, or null when nothing valid was stored. */
export function readStoredLightboxMode(): LightboxMode | null {
  try {
    const value = window.localStorage.getItem(LIGHTBOX_MODE_STORAGE_KEY);
    if (value !== null && (VALID_MODES as ReadonlyArray<string>).includes(value)) {
      return value as LightboxMode;
    }
  } catch {
    // Storage unavailable; the default mode applies instead.
  }
  return null;
}

/** Remember a mode; failures are silent by design. */
export function writeStoredLightboxMode(mode: LightboxMode): void {
  try {
    window.localStorage.setItem(LIGHTBOX_MODE_STORAGE_KEY, mode);
  } catch {
    // Not persistable right now — the session still works.
  }
}
