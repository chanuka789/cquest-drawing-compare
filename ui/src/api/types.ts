/**
 * Types that mirror the Pydantic models in `engine/core/models.py` and the
 * response models in `engine/api/`.
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

// ── Intake ─────────────────────────────────────────────────────────────

export type IssueSide = 'old' | 'new';

/** Whether the current issue is the whole set or only what changed. */
export type IssueType = 'full' | 'partial' | 'unknown';

/** How a drawing number was found. Shown so the user knows what to trust. */
export type NumberSource =
  | 'titleblock'
  | 'sheet_text'
  | 'filename'
  | 'drawing_list'
  | 'user'
  | 'ai'
  | 'none';

/** State of one issue folder. */
export interface SideState {
  side: IssueSide;
  folder: string | null;
  headline: string;
  file_count: number;
  sheet_count: number;
  identified_count: number;
  attention_count: number;
  is_scanning: boolean;
  error: string | null;
  other_files: Record<string, number>;
  skipped_count: number;
}

/** One drawing sheet in a folder panel's list. */
export interface SheetRow {
  abs_path: string;
  filename: string;
  page_index: number;
  page_count: number;
  /** A drawing issued as 'Sheet 1 of 3' is one drawing, not three. */
  sheets_in_drawing: number;
  drawing_no: string | null;
  title: string | null;
  revision: string | null;
  scale: string | null;
  sheet_size: string | null;
  source_of_number: NumberSource;
  source_explanation: string;
  number_mismatch: boolean;
  is_readable: boolean;
  looks_scanned: boolean;
  warnings: string[];
}

/** A file that could not be read, and what to do about it. */
export interface QuarantinedFile {
  path: string;
  filename: string;
  reason: string;
  label: string;
  detail: string;
  advice: string;
}

// ── The output folder ──────────────────────────────────────────────────

export interface OutputValidation {
  path: string;
  exists: boolean;
  is_writable: boolean;
  is_inside_input: boolean;
  is_same_as_input: boolean;
  is_empty: boolean;
  free_bytes: number;
  errors: string[];
  warnings: string[];
  is_valid: boolean;
}

// ── The register ───────────────────────────────────────────────────────

export type RegisterStatus =
  | 'revised'
  | 'unchanged'
  | 'same_rev_different_file'
  | 'new'
  | 'not_reissued'
  | 'removed'
  | 'superseded_in_folder'
  | 'duplicate_file'
  | 'unidentified'
  | 'unreadable'
  | 'in_list_not_in_folder'
  | 'in_folder_not_in_list'
  | 'status_change';

/** One line of the register: a drawing, across both issues. */
export interface RegisterRow {
  drawing_no: string;
  status: RegisterStatus;
  title: string | null;
  old_revision: string | null;
  new_revision: string | null;
  old_path: string | null;
  new_path: string | null;
  old_page: number | null;
  new_page: number | null;
  source_of_number: NumberSource;
  number_mismatch: boolean;
  needs_attention: boolean;
  note: string;
  in_drawing_list: boolean | null;
  superseded_paths: string[];
  duplicate_paths: string[];
}

export interface RegisterSummary {
  counts: Partial<Record<RegisterStatus, number>>;
  old_sheet_count: number;
  new_sheet_count: number;
  issue_type: IssueType;
  attention_count: number;
  comparable_count: number;
  sentence: string;
}

/** Asked instead of guessing when the two sets are very different sizes. */
export interface IssueTypeQuestion {
  old_count: number;
  new_count: number;
  question: string;
  options: { id: string; label: string; description: string }[];
}

export interface ReconcileResult {
  rows: RegisterRow[];
  summary: RegisterSummary;
  needs_issue_type_confirmation: boolean;
  issue_type_question: IssueTypeQuestion | null;
}

// ── The drawing list ───────────────────────────────────────────────────

export interface ParsedListRow {
  row_number: number;
  drawing_no: string;
  title: string | null;
  revision: string | null;
  status: string | null;
  date: string | null;
}

export interface ListParseResult {
  source_path: string;
  sheet_name: string | null;
  sheet_names: string[];
  header_row: number | null;
  mapping: Record<string, string>;
  columns: string[];
  preview: ParsedListRow[];
  row_count: number;
  is_revision_matrix: boolean;
  revision_columns: string[];
  warnings: string[];
  confidence: number;
  ok: boolean;
}

// ── Progress ───────────────────────────────────────────────────────────

export type ProgressStage = 'scan' | 'inspect' | 'extract' | 'reconcile' | 'export';
export type ProgressKind = 'started' | 'progress' | 'finished' | 'failed' | 'cancelled';

export interface ProgressEvent {
  run_id: string;
  stage: ProgressStage;
  kind: ProgressKind;
  current: number;
  total: number;
  current_item: string;
  elapsed: number;
  message: string;
  fraction: number;
  eta: number | null;
  detail: Record<string, unknown>;
}

// ── Options ────────────────────────────────────────────────────────────

/** A shipped or user-saved sheet profile. */
export interface SheetProfileOption {
  id: string;
  label: string;
}

/** Tolerance is always in millimetres at drawing scale, never in pixels. */
export interface ToleranceOption {
  id: string;
  label: string;
  millimetres: number;
}

// ── Matching ───────────────────────────────────────────────────────────

export type MatchRunState = 'idle' | 'running' | 'ready' | 'failed';

/** How a pair of sheets was matched, recorded so the user can judge it. */
export type MatchTier =
  | 'exact_number'
  | 'normalised_number'
  | 'normalised_name'
  | 'fuzzy_name'
  | 'content_fingerprint';

/** Lifecycle of a matching run: idle → running → ready (or failed). */
export interface MatchStatus {
  state: MatchRunState;
  run_id: string | null;
  current: number;
  total: number;
  message: string | null;
}

/** One drawing sheet as the matching endpoints talk about it. */
export interface SheetBrief {
  abs_path: string;
  filename: string;
  drawing_no: string | null;
  title: string | null;
  revision: string | null;
  page_index: number;
  /** `<abs_path>#<page_index>` — how a sheet is identified in decisions. */
  key: string;
}

/** A different sheet the matcher thought might be the right target. */
export interface MatchAlternative {
  sheet: SheetBrief;
  confidence: number;
  reason: string;
}

/** One proposed pairing of a previous-issue sheet with a current-issue sheet. */
export interface MatchPair {
  old: SheetBrief;
  new: SheetBrief;
  confidence: number;
  tier: MatchTier;
  reason: string;
  ambiguous: boolean;
  needs_review: boolean;
  alternatives: MatchAlternative[];
}

export interface MatchSummary {
  auto: number;
  review: number;
  old_unmatched: number;
  new_unmatched: number;
}

/** Full result of a matching run. */
export interface MatchResult {
  pairs: MatchPair[];
  old_unmatched: SheetBrief[];
  new_unmatched: SheetBrief[];
  /** Paths of older revisions ignored while matching; not shown yet. */
  superseded: string[];
  summary: MatchSummary;
  notes: string[];
}

/** The engine's answer after the user's decisions are posted. */
export interface MatchDecisionsResponse {
  accepted: number;
  rejected: number;
  manual: number;
  unresolved_review: number;
}
