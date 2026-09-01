/**
 * State for the Register screen.
 *
 * The register is built on the engine, not here. This store holds the result,
 * the filters the user applies to it, and the selected row.
 */

import { create } from 'zustand';

import {
  ApiError,
  buildRegister,
  correctDrawingNumber,
  exportRegister,
} from '../api/client';
import type {
  IssueType,
  IssueTypeQuestion,
  RegisterRow,
  RegisterStatus,
  RegisterSummary,
} from '../api/types';

export type SortKey = 'drawing_no' | 'status' | 'old_revision' | 'new_revision';

const EMPTY_SUMMARY: RegisterSummary = {
  counts: {},
  old_sheet_count: 0,
  new_sheet_count: 0,
  issue_type: 'unknown',
  attention_count: 0,
  comparable_count: 0,
  sentence: '',
};

interface RegisterState {
  rows: RegisterRow[];
  summary: RegisterSummary;
  loading: boolean;
  error: string | null;

  /** Set when the engine needs the issue type before it will build anything. */
  question: IssueTypeQuestion | null;
  issueType: IssueType;

  search: string;
  statusFilter: RegisterStatus | null;
  attentionOnly: boolean;
  sortKey: SortKey;
  sortAscending: boolean;
  selected: RegisterRow | null;

  exportedPath: string | null;
  notice: string | null;

  build: (issueType?: IssueType) => Promise<void>;
  answerIssueType: (answer: string) => Promise<void>;
  setSearch: (text: string) => void;
  setStatusFilter: (status: RegisterStatus | null) => void;
  toggleAttentionOnly: () => void;
  setSort: (key: SortKey) => void;
  select: (row: RegisterRow | null) => void;
  correct: (row: RegisterRow, drawingNo: string, applyToAll: boolean) => Promise<void>;
  exportToExcel: () => Promise<void>;
  setNotice: (message: string | null) => void;
}

function message(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Something went wrong. The details are in the log file.';
}

export const useRegisterStore = create<RegisterState>((set, get) => ({
  rows: [],
  summary: EMPTY_SUMMARY,
  loading: false,
  error: null,

  question: null,
  issueType: 'unknown',

  search: '',
  statusFilter: null,
  attentionOnly: false,
  sortKey: 'drawing_no',
  sortAscending: true,
  selected: null,

  exportedPath: null,
  notice: null,

  build: async (issueType) => {
    const chosen = issueType ?? get().issueType;
    set({ loading: true, error: null, notice: null });

    try {
      const result = await buildRegister(chosen);

      if (result.needs_issue_type_confirmation) {
        // No register until the question is answered: guessing here would
        // make every old-only row say the wrong thing.
        set({
          question: result.issue_type_question,
          rows: [],
          summary: EMPTY_SUMMARY,
          loading: false,
        });
        return;
      }

      set({
        rows: result.rows,
        summary: result.summary,
        question: null,
        issueType: result.summary.issue_type,
        loading: false,
      });
    } catch (error) {
      set({ error: message(error), loading: false });
    }
  },

  answerIssueType: async (answer) => {
    // "Compare only what was reissued" is a partial issue with a clearer name.
    const issueType: IssueType = answer === 'full' ? 'full' : 'partial';
    set({ issueType, question: null });
    await get().build(issueType);
  },

  setSearch: (search) => set({ search }),
  setStatusFilter: (statusFilter) =>
    set({ statusFilter: get().statusFilter === statusFilter ? null : statusFilter }),
  toggleAttentionOnly: () => set({ attentionOnly: !get().attentionOnly }),

  setSort: (key) =>
    set(
      get().sortKey === key
        ? { sortAscending: !get().sortAscending }
        : { sortKey: key, sortAscending: true },
    ),

  select: (selected) => set({ selected }),

  correct: async (row, drawingNo, applyToAll) => {
    const path = row.new_path ?? row.old_path;
    const page = row.new_page ?? row.old_page ?? 0;
    if (!path) return;

    try {
      const result = await correctDrawingNumber({
        abs_path: path,
        page_index: page,
        drawing_no: drawingNo,
        apply_to_all: applyToAll,
      });

      set({
        selected: null,
        notice: result.also_applied
          ? `Applied to ${result.also_applied} other ${
              result.also_applied === 1 ? 'sheet' : 'sheets'
            }.`
          : null,
      });
      await get().build();
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  exportToExcel: async () => {
    set({ notice: null });
    try {
      const result = await exportRegister();
      set({ exportedPath: result.path, notice: `Register saved to ${result.path}` });
    } catch (error) {
      set({ notice: message(error) });
    }
  },

  setNotice: (notice) => set({ notice }),
}));

/** The rows actually shown, after filters and sorting. */
export function visibleRows(state: RegisterState): RegisterRow[] {
  const text = state.search.trim().toLowerCase();

  let rows = state.rows;
  if (state.statusFilter) rows = rows.filter((row) => row.status === state.statusFilter);
  if (state.attentionOnly) rows = rows.filter((row) => row.needs_attention);
  if (text) {
    rows = rows.filter((row) =>
      [row.drawing_no, row.title, row.note]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(text)),
    );
  }

  const direction = state.sortAscending ? 1 : -1;
  return [...rows].sort((left, right) => {
    // Rows needing attention stay at the top whatever the sort, because they
    // are the reason to open the register at all.
    if (left.needs_attention !== right.needs_attention) return left.needs_attention ? -1 : 1;

    const a = String(left[state.sortKey] ?? '');
    const b = String(right[state.sortKey] ?? '');
    return a.localeCompare(b) * direction;
  });
}
