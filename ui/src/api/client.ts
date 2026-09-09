/**
 * Typed fetch client for the local engine.
 *
 * Finding the engine:
 *  - Inside the desktop shell the port is random, so it is read once from
 *    `window.pywebview.api.get_api_port()`.
 *  - In production the UI is served by the engine itself, so a same-origin
 *    relative URL is correct.
 *  - In a browser during development, fall back to 127.0.0.1:8000, which is
 *    where `uvicorn engine.api.app:app --port 8000` listens. Override with
 *    VITE_API_BASE if you run it somewhere else.
 */

import type {
  ErrorResponse,
  HealthResponse,
  IssueSide,
  IssueType,
  ListParseResult,
  MatchDecisionsResponse,
  MatchResult,
  MatchStatus,
  OutputValidation,
  QuarantinedFile,
  ReconcileResult,
  RenameContext,
  RenameMode,
  RenamePlan,
  RenameStatus,
  RenameTemplate,
  SheetProfileOption,
  SheetRow,
  SideState,
  TileManifest,
} from './types';
import { getApiPort, waitForBridge } from '../lib/native';

const DEV_FALLBACK_BASE = 'http://127.0.0.1:8000';

/** A structured error from the engine. `code` is stable; `message` is for the user. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail: Record<string, unknown>;

  constructor(code: string, message: string, status: number, detail: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.detail = detail;
  }

  /** True when the engine could not be reached at all. */
  get isOffline(): boolean {
    return this.code === 'engine_unreachable';
  }
}

let basePromise: Promise<string> | null = null;
/** The base once any request has resolved it; null until then. */
let resolvedBase: string | null = null;

async function resolveBaseOnce(): Promise<string> {
  const configured = import.meta.env.VITE_API_BASE;
  if (typeof configured === 'string' && configured.length > 0) {
    return configured.replace(/\/$/, '');
  }

  const hasBridge = await waitForBridge();
  if (hasBridge) {
    const port = await getApiPort();
    if (port !== null) return `http://127.0.0.1:${port}`;
  }

  // Served by the engine itself: same origin, no port juggling.
  if (!import.meta.env.DEV) return '';

  return DEV_FALLBACK_BASE;
}

async function resolveBase(): Promise<string> {
  basePromise ??= resolveBaseOnce().then((base) => {
    resolvedBase = base;
    return base;
  });
  return basePromise;
}

/** The engine's base URL, resolved once per session. */
export function apiBase(): Promise<string> {
  return resolveBase();
}

/**
 * The resolved engine base, or null before the first request finished.
 * URL builders that must stay synchronous (tile URLs) use this; callers
 * fetch a manifest first, which resolves the base, so the null window
 * never matters in practice.
 */
export function apiBaseSync(): string | null {
  return resolvedBase;
}

/** Forget the cached base URL. Used by tests and after a reconnect. */
export function resetApiBase(): void {
  basePromise = null;
  resolvedBase = null;
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = (value as { error?: unknown }).error;
  return (
    typeof candidate === 'object' &&
    candidate !== null &&
    typeof (candidate as { code?: unknown }).code === 'string'
  );
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // A non-JSON body means something below the application answered.
  }

  if (isErrorResponse(body)) {
    const { code, message, detail } = body.error;
    return new ApiError(code, message, response.status, detail ?? {});
  }

  return new ApiError(
    `http_${response.status}`,
    `The engine returned an unexpected response (HTTP ${response.status}).`,
    response.status,
  );
}

/** Perform a request against the engine and return the parsed JSON body. */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = await apiBase();
  const url = `${base}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
    });
  } catch (cause) {
    throw new ApiError(
      'engine_unreachable',
      'The engine is not responding. Close the application and start it again.',
      0,
      { url, cause: String(cause) },
    );
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;

  return (await response.json()) as T;
}

export function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'GET' });
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Ask the engine whether it is running, and what it is. */
export function fetchHealth(): Promise<HealthResponse> {
  return get<HealthResponse>('/api/health');
}

export function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' });
}

// ── Intake ─────────────────────────────────────────────────────────────

/** Choose an issue folder. Returns as soon as the fast pass is done. */
export function setFolder(side: IssueSide, folder: string): Promise<SideState> {
  return post<SideState>('/api/folder', { side, folder });
}

/** Start the deep pass. Progress arrives on the WebSocket. */
export function startScan(side: IssueSide): Promise<{ run_id: string }> {
  return post<{ run_id: string }>(`/api/scan/${side}`);
}

export function cancelScan(): Promise<{ cancelled: boolean }> {
  return post<{ cancelled: boolean }>('/api/scan/cancel');
}

export function fetchSides(): Promise<SideState[]> {
  return get<SideState[]>('/api/sides');
}

export function fetchSheets(side: IssueSide): Promise<SheetRow[]> {
  return get<SheetRow[]>(`/api/sheets/${side}`);
}

export function fetchQuarantine(): Promise<QuarantinedFile[]> {
  return get<QuarantinedFile[]>('/api/quarantine');
}

// ── Output folder ──────────────────────────────────────────────────────

export function fetchOutputSuggestion(): Promise<{ folder: string | null }> {
  return get<{ folder: string | null }>('/api/output/suggestion');
}

export function validateOutput(folder: string): Promise<OutputValidation> {
  return post<OutputValidation>('/api/output/validate', { folder });
}

export function setOutput(folder: string): Promise<{ workspace: Record<string, string> }> {
  return post<{ workspace: Record<string, string> }>('/api/output', { folder });
}

// ── Options ────────────────────────────────────────────────────────────

export function fetchProfiles(): Promise<SheetProfileOption[]> {
  return get<SheetProfileOption[]>('/api/profiles');
}

export function setOptions(options: {
  profile_id?: string;
  tolerance_mm?: number;
}): Promise<{ profile_id: string; tolerance_mm: number }> {
  return post('/api/options', options);
}

export function resetSession(): Promise<{ reset: boolean }> {
  return post<{ reset: boolean }>('/api/session/reset');
}

// ── The drawing list ───────────────────────────────────────────────────

export function previewDrawingList(path: string): Promise<ListParseResult> {
  return post<ListParseResult>('/api/drawing-list/preview', { path });
}

export function applyListMapping(
  mapping: Record<string, string>,
  sheetName?: string,
): Promise<ListParseResult> {
  return post<ListParseResult>('/api/drawing-list/mapping', {
    mapping,
    sheet_name: sheetName ?? null,
  });
}

export function clearDrawingList(): Promise<{ cleared: boolean }> {
  return del<{ cleared: boolean }>('/api/drawing-list');
}

// ── The register ───────────────────────────────────────────────────────

export function buildRegister(
  issueType: IssueType,
  answer?: string,
): Promise<ReconcileResult> {
  return post<ReconcileResult>('/api/register', {
    issue_type: issueType,
    answer: answer ?? null,
  });
}

export function correctDrawingNumber(body: {
  abs_path: string;
  page_index: number;
  drawing_no: string;
  apply_to_all: boolean;
}): Promise<{ corrected: number; also_applied: number }> {
  return post('/api/register/correct', body);
}

export function exportRegister(): Promise<{
  path: string;
  folder: string;
  rows: number;
  filename: string;
}> {
  return post('/api/report/register');
}

// ── Matching ───────────────────────────────────────────────────────────

export function getMatchStatus(): Promise<MatchStatus> {
  return get<MatchStatus>('/api/match/status');
}

/** Start a matching run. Progress is read back from the status endpoint. */
export function runMatch(): Promise<{ run_id: string }> {
  return post<{ run_id: string }>('/api/match/run');
}

/** The engine answers only once its run is ready — poll status first. */
export function fetchMatchResult(): Promise<MatchResult> {
  return get<MatchResult>('/api/match/result');
}

/** A pair is identified by its old sheet key; manual pairs link two keys. */
export function postMatchDecisions(decisions: {
  accepted: string[];
  rejected: string[];
  manual: { old_key: string; new_key: string }[];
}): Promise<MatchDecisionsResponse> {
  return post<MatchDecisionsResponse>('/api/match/decisions', decisions);
}

/** Write the workspace audit JSON. 422 when no output folder is chosen. */
export function finalizeMatch(): Promise<{ path: string | null }> {
  return post<{ path: string | null }>('/api/match/finalize');
}

// ── Rename ─────────────────────────────────────────────────────────────

export function fetchRenameTemplates(): Promise<RenameTemplate[]> {
  return get<RenameTemplate[]>('/api/rename/templates');
}

/** Fixed template tokens (project, originator) from the sheet profile. */
export function fetchRenameContext(): Promise<RenameContext> {
  return get<RenameContext>('/api/rename/profile-context');
}

/** Dry-run the template and return the full plan. 422 when nothing to rename. */
export function postRenamePlan(body: {
  template: string;
  mode: RenameMode;
  use_discipline_folders: boolean;
  overrides: Record<string, string>;
}): Promise<RenamePlan> {
  return post<RenamePlan>('/api/rename/plan', body);
}

/** Apply the plan the session holds. The UI re-posts the plan first. */
export function applyRename(iUnderstand: boolean): Promise<{ run_id: string }> {
  return post<{ run_id: string }>('/api/rename/apply', {
    i_understand: iUnderstand,
  });
}

/** Where the rename or undo run has got to — polled, not a socket. */
export function fetchRenameStatus(): Promise<RenameStatus> {
  return get<RenameStatus>('/api/rename/status');
}

export function cancelRename(): Promise<{ cancelled: boolean }> {
  return post<{ cancelled: boolean }>('/api/rename/cancel');
}

/** Reverse the last apply run, verifying each file's hash first. */
export function undoRename(): Promise<{ run_id: string }> {
  return post<{ run_id: string }>('/api/rename/undo');
}

// ── Tiles ──────────────────────────────────────────────────────────────

/** One tile's path. `sheet_id` is opaque, so it must be escaped for a URL. */
function tilePath(sheetId: string, level: number, x: number, y: number): string {
  return `/api/tiles/${encodeURIComponent(sheetId)}/${level}/${x}/${y}.png`;
}

/** What tile levels exist for one rendered sheet. */
export function fetchTileManifest(sheetId: string): Promise<TileManifest> {
  return get<TileManifest>(`/api/tiles/${encodeURIComponent(sheetId)}/manifest`);
}

/**
 * The URL of one tile, for image loading and as a cache key — no fetch.
 *
 * Synchronous because the tile loader builds lists of visible tiles without
 * awaiting anything. The base URL is only known after the first API request
 * of the session, but the loader always fetches the manifest first, which
 * resolves it, so the bare relative fallback below never leaks out there.
 */
export function tileUrl(sheetId: string, level: number, x: number, y: number): string {
  const path = tilePath(sheetId, level, x, y);
  const base = apiBaseSync();
  return base === null ? path : `${base}${path}`;
}

/**
 * Download one tile as a PNG blob with the same error handling as `request`.
 * `signal` lets the tile loader cancel stale work; an abort is not an error.
 */
export async function fetchTileBlob(
  sheetId: string,
  level: number,
  x: number,
  y: number,
  signal?: AbortSignal,
): Promise<Blob> {
  const base = await apiBase();
  const url = `${base}${tilePath(sheetId, level, x, y)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      signal,
      headers: { Accept: 'image/png' },
    });
  } catch (cause) {
    // An abort means the caller cancelled stale work, never an engine fault.
    if (signal?.aborted) throw cause;
    throw new ApiError(
      'engine_unreachable',
      'The engine is not responding. Close the application and start it again.',
      0,
      { url, cause: String(cause) },
    );
  }

  if (!response.ok) throw await toApiError(response);
  return response.blob();
}
