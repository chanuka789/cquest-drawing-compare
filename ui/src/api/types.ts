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

// ── Tiles (Phase 4 lightbox viewer) ────────────────────────────────────

/** One resolution level of a rendered sheet's tile pyramid. */
export interface TileLevel {
  level: number;
  /** Tiles across the sheet at this level (level 0 is the single-tile thumbnail). */
  cols: number;
  rows: number;
  tile_count: number;
}

/** Manifest of a rendered sheet: the levels the viewer may ask for. */
export interface TileManifest {
  /** Opaque sheet identifier issued by the engine — never derived in the UI. */
  sheet_id: string;
  /** Resolution the sheet was rendered at, in dots per inch (default 200). */
  dpi: number;
  /** Full-resolution sheet size in pixels. */
  width_px: number;
  height_px: number;
  /** Levels in ascending order, from the coarse thumbnail up to full size. */
  levels: TileLevel[];
  /** Edge length of one tile in pixels (right and bottom edge tiles clip). */
  tile_size: number;
}

// ── Rename ─────────────────────────────────────────────────────────────

/** Where a rename writes its result: a copy or the file itself. */
export type RenameMode = 'copy' | 'in_place';

/** Every verdict the planner can give one planned rename. */
export type RenameActionStatus =
  | 'ok'
  | 'unchanged'
  | 'collision'
  | 'invalid_name'
  | 'path_too_long'
  | 'missing_tokens'
  | 'locked'
  | 'reserved_name'
  | 'source_missing';

/** One naming template preset, from GET /api/rename/templates. */
export interface RenameTemplate {
  id: string;
  label: string;
  template: string;
  description: string;
}

/** Fixed profile values the fixed template tokens resolve against. */
export interface RenameContext {
  project?: string | null;
  originator?: string | null;
  status?: string | null;
}

/** One planned rename: what is copied or moved, and to where. */
export interface RenameAction {
  source_path: string;
  target_path: string;
  target_folder: string;
  status: RenameActionStatus;
  old_name: string;
  new_name: string;
  warnings: string[];
}

/** Per-status counts plus the grouped numbers the summary bar needs. */
export interface RenameSummary {
  ok: number;
  unchanged: number;
  collision: number;
  invalid_name: number;
  path_too_long: number;
  missing_tokens: number;
  locked: number;
  reserved_name: number;
  source_missing: number;
  /** Same number as `ok`; how many files would actually move or copy. */
  to_change: number;
  /** Anything that is not ok or unchanged — needs a human decision. */
  problems: number;
}

/** The full dry run: every action, the counts, and whether it may apply. */
export interface RenamePlan {
  actions: RenameAction[];
  summary: RenameSummary;
  can_apply: boolean;
  /** Plan-level errors, shown verbatim; they block applying. */
  errors: string[];
  mode: RenameMode;
  /** Where copies land in copy mode; carried as metadata in in-place mode. */
  output_dir: string;
  /** Up to three real rendered file names for the live template preview. */
  preview: string[];
}

/** What a rename or undo run is doing. */
export type RenameRunKind = 'rename' | 'undo';

/** Lifecycle of a rename or undo run on the engine. */
export type RenameRunState = 'idle' | 'running' | 'done' | 'failed' | 'cancelled';

/** One file an apply or undo run could not handle. */
export interface RenameFailedItem {
  path: string;
  error?: string;
  reason?: string;
  note?: string;
}

/** Response of GET /api/rename/status. */
export interface RenameStatus {
  kind: RenameRunKind | null;
  state: RenameRunState;
  run_id: string | null;
  current: number;
  total: number;
  current_item: string | null;
  completed: number;
  failed: number;
  message: string | null;
  error: string | null;
  failed_items: RenameFailedItem[];
  undo_log_path: string | null;
}

// ── Alignment (Phase 4) ────────────────────────────────────────────────

/** Lifecycle of a batch alignment run on the engine. */
export type AlignRunState = 'idle' | 'running' | 'done' | 'failed' | 'cancelled';

/** Quality verdict of one aligned pair. */
export type AlignVerdict = 'excellent' | 'good' | 'poor' | 'failed';

/** One endpoint of an alignment result row: which sheet of which issue. */
export interface AlignSheetRef {
  filename: string;
  /** Engine tile-sheet id; null when the pair has no readable sheet. */
  sheet_id: string | null;
}

/** One quality-gate metric: value, its threshold and whether it passed. */
export interface AlignMetric {
  value: number;
  threshold: number;
  passed: boolean;
}

/** One aligned (or failed) pair, as the review screen shows it. */
export interface AlignResultRow {
  old: AlignSheetRef;
  new: AlignSheetRef;
  verdict: AlignVerdict;
  /** Which automatic strategy produced the fit, e.g. "text_anchors". */
  method: string;
  /** The engine's own note — failure reasons are shown verbatim. */
  note: string;
  duration_s: number;
  /** Row-major 3×3 matrix, old-sheet px → new-sheet px at 200 dpi; null when nothing fitted. */
  matrix: number[][] | null;
  /** Plain-English explanation of the verdict, for the user. */
  explanation: string;
  rms_mm_on_paper: number | null;
  rms_mm_on_site: number | null;
  /** metric name → value/threshold/passed, e.g. "rms_residual_px". */
  metrics: Record<string, AlignMetric> | null;
}

/** Response of GET /api/align/results (available once the run is done). */
export interface AlignResultsPayload {
  results: AlignResultRow[];
  summary: Record<string, number>;
}

/** Response of GET /api/align/status — polled, not a socket. */
export interface AlignStatus {
  state: AlignRunState;
  run_id: string | null;
  current: number;
  total: number;
  current_label: string | null;
  message: string | null;
  error: string | null;
  /** verdict → count once a run has settled. */
  summary: Record<string, number>;
}

/** One user-clicked correspondence for manual alignment, in sheet pixels at 200 dpi. */
export interface ManualPoint {
  old_x: number;
  old_y: number;
  new_x: number;
  new_y: number;
}

// ── Changes (Phase 5) ──────────────────────────────────────────────────

/** Lifecycle of a batch comparison run on the engine. */
export type CompareRunState = 'idle' | 'running' | 'done' | 'failed' | 'cancelled';

/** What kind of change one region is. */
export type ChangeType = 'added' | 'removed' | 'moved' | 'modified' | 'cosmetic';

/** How much the change matters. Ranked by consequence, never by area alone. */
export type ChangeSeverity = 'critical' | 'major' | 'minor' | 'trivial';

/** What the words in a region are, when the text layer could be read. */
export type TextChangeKind = 'dimension' | 'tag' | 'note';

/** One rectangle of the new sheet that differs from the old one. */
export interface ChangeRegion {
  index: number;
  type: ChangeType;
  severity: ChangeSeverity;
  /** x, y, width, height on the new sheet's pixel grid — for drawing only. */
  bbox_px: [number, number, number, number];
  /** The same box on paper, in millimetres. */
  bbox_mm: [number, number, number, number];
  area_mm2: number;
  /** Area at drawing scale; null when the scale could not be read. */
  area_site_mm2: number | null;
  added_px: number;
  removed_px: number;
  is_cosmetic: boolean;
  moved_from_px: [number, number, number, number] | null;
  moved_by_mm: number | null;
  moved_by_site_mm: number | null;
  text_kind: TextChangeKind | null;
  old_text: string | null;
  new_text: string | null;
  /** One sentence for the user. Never mentions pixels. */
  explanation: string;
}

/** One compared pair and everything found on it. */
export interface CompareResultRow {
  old: AlignSheetRef;
  new: AlignSheetRef;
  width_px: number;
  height_px: number;
  dpi: number;
  scale_denominator: number | null;
  /** change type → count. */
  counts: Record<string, number>;
  region_count: number;
  /** Regions that are not merely cosmetic. */
  substantive_count: number;
  truncated: boolean;
  duration_s: number;
  /** Set when the pair could not be compared at all. */
  failure: string | null;
  regions: ChangeRegion[];
}

/** Response of GET /api/compare/results. */
export interface CompareResultsPayload {
  results: CompareResultRow[];
  summary: Record<string, number>;
}

/** Response of GET /api/compare/status — polled, not a socket. */
export interface CompareStatus {
  state: CompareRunState;
  run_id: string | null;
  current: number;
  total: number;
  current_label: string | null;
  message: string | null;
  error: string | null;
  summary: Record<string, number>;
}

/** What POST /api/compare/start kicked off — it may need to align first. */
export interface CompareStartResponse {
  stage: 'aligning' | 'comparing';
  run_id: string | null;
}

/** Response of GET /api/output — lets the UI restore the chosen folder. */
export interface OutputState {
  folder: string | null;
  workspace: Record<string, string> | null;
}
