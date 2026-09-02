/**
 * The pywebview bridge.
 *
 * Two channels, two jobs. Everything — projects, register, changes, progress —
 * goes over HTTP. This bridge is used only for the things a browser genuinely
 * cannot do: open a real Windows folder or file dialog, and report which port
 * the engine is listening on.
 *
 * Every function here degrades gracefully, so the whole UI can be developed
 * and tested in a plain browser without launching the desktop window.
 */

interface PywebviewApi {
  get_api_port: () => Promise<number>;
  pick_folder: (title?: string) => Promise<string | null>;
  pick_file: (title?: string, fileTypes?: string[]) => Promise<string | null>;
}

declare global {
  interface Window {
    pywebview?: { api?: Partial<PywebviewApi> };
  }
}

/** True when running inside the desktop shell rather than a browser tab. */
export function isDesktop(): boolean {
  return typeof window !== 'undefined' && Boolean(window.pywebview?.api);
}

/**
 * Wait for the bridge to be injected.
 *
 * pywebview attaches `window.pywebview.api` shortly after the page loads and
 * fires `pywebviewready`. Resolves to false in a browser, where it never comes.
 */
export function waitForBridge(timeoutMs = 3000): Promise<boolean> {
  if (isDesktop()) return Promise.resolve(true);

  return new Promise((resolve) => {
    let settled = false;

    const finish = (available: boolean): void => {
      if (settled) return;
      settled = true;
      window.removeEventListener('pywebviewready', onReady);
      window.clearTimeout(timer);
      resolve(available);
    };

    const onReady = (): void => finish(true);
    const timer = window.setTimeout(() => finish(isDesktop()), timeoutMs);

    window.addEventListener('pywebviewready', onReady);
  });
}

/** The engine port reported by the shell, or null in a browser. */
export async function getApiPort(): Promise<number | null> {
  const api = window.pywebview?.api;
  if (!api?.get_api_port) return null;
  try {
    return await api.get_api_port();
  } catch {
    return null;
  }
}

/**
 * Open the native folder dialog.
 *
 * Returns the chosen absolute path, or null if the user cancelled or the app
 * is running in a browser where no native dialog exists.
 */
export async function pickFolder(title = 'Choose a folder'): Promise<string | null> {
  const api = window.pywebview?.api;
  if (!api?.pick_folder) throw new NativeError(NO_BRIDGE_MESSAGE);

  try {
    return (await api.pick_folder(title)) ?? null;
  } catch (cause) {
    throw new NativeError(DIALOG_FAILED_MESSAGE, cause);
  }
}

/** Open the native file dialog. `fileTypes` uses the pywebview filter form. */
export async function pickFile(
  title = 'Choose a file',
  fileTypes: string[] = [],
): Promise<string | null> {
  const api = window.pywebview?.api;
  if (!api?.pick_file) throw new NativeError(NO_BRIDGE_MESSAGE);

  try {
    return (await api.pick_file(title, fileTypes)) ?? null;
  } catch (cause) {
    throw new NativeError(DIALOG_FAILED_MESSAGE, cause);
  }
}

/** A dialog could not be opened. Carries the cause for the log, not the user. */
export class NativeError extends Error {
  override readonly cause: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = 'NativeError';
    this.cause = cause;
  }
}

/** Shown in a plain browser tab, where there is no native dialog to open. */
export const NO_BRIDGE_MESSAGE =
  'Choosing a folder needs the desktop application. Open C-Quest Drawing Compare ' +
  'rather than a browser tab.';

/** Shown when the dialog itself failed, which is a fault worth reporting. */
export const DIALOG_FAILED_MESSAGE =
  'The folder chooser could not be opened. Close the application and start it ' +
  'again; if it keeps happening, the details are in the log file.';
