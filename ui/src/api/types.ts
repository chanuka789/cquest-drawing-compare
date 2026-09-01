/**
 * Types that mirror the Pydantic models in `engine/core/models.py`.
 * If a field changes there, change it here in the same commit.
 */

/** Chosen by the user in Settings. Offline is the default and never changes itself. */
export type AiMode = 'offline' | 'local' | 'cloud';

/** Where the application keeps its own data on this machine. */
export interface AppPathsInfo {
  root: string;
  db: string;
  logs: string;
  cache: string;
  profiles: string;
}

/** Response of `GET /api/health`. */
export interface HealthResponse {
  status: string;
  app_name: string;
  version: string;
  dev_mode: boolean;
  ai_mode: AiMode;
  schema_version: number;
  paths: AppPathsInfo;
}

/** The inner object of every failed request. */
export interface ErrorBody {
  code: string;
  message: string;
  detail: Record<string, unknown>;
}

/** Every failed request returns this shape. */
export interface ErrorResponse {
  error: ErrorBody;
}

/** Set reconciliation result for one sheet. Mirrors `PairStatus`. */
export type PairStatus = 'matched' | 'new' | 'missing' | 'ambiguous' | 'duplicate';

/** Tolerance is always in millimetres at drawing scale, never in pixels. */
export interface ToleranceOption {
  id: string;
  label: string;
  millimetres: number;
}

/** A shipped or user-saved sheet profile. */
export interface SheetProfileOption {
  id: string;
  label: string;
}
