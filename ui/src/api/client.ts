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

import type { ErrorResponse, HealthResponse } from './types';
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

async function resolveBase(): Promise<string> {
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

/** The engine's base URL, resolved once per session. */
export function apiBase(): Promise<string> {
  basePromise ??= resolveBase();
  return basePromise;
}

/** Forget the cached base URL. Used by tests and after a reconnect. */
export function resetApiBase(): void {
  basePromise = null;
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
