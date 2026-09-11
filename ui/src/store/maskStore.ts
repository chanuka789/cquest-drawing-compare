/**
 * State for the mask editor (Phase 5, Task 5.4).
 *
 * The flow the whole masking design exists for: the engine clusters the new
 * issue's sheets into two or three templates and detects the zones once per
 * cluster; this store holds those zones while the user adjusts them, and
 * applies the confirmed result to every sheet in the cluster in one action.
 *
 * Two rules the store enforces so the screen cannot break them:
 *
 *  - **Disabled is not deleted.** Switching a zone off keeps its detection
 *    and its evidence, so turning it back on costs one click and the editor
 *    can still explain what it was.
 *  - **Protected beats masked.** The protected list is separate and is never
 *    editable into an exclusion; a region the application deliberately kept
 *    comparing — north arrow, scale bar, key plan, general notes — stays
 *    compared whatever overlaps it.
 */

import { create } from 'zustand';

import { ApiError, fetchMaskTemplates, saveMask } from '../api/client';
import type { FracRect, MaskZone, ProtectedRegion, SheetTemplate, ZoneType } from '../api/types';

/** A new zone drawn by the user always starts as this type. */
const USER_ZONE: ZoneType = 'user';

export type MaskTool = 'select' | 'rectangle' | 'protect';

export interface MaskState {
  loading: boolean;
  saving: boolean;
  errorMessage: string | null;
  templates: SheetTemplate[];
  /** Which template the editor is showing. */
  activeId: string | null;
  /** Working copies, so cancelling leaves the engine's detection untouched. */
  zones: MaskZone[];
  protectedRegions: ProtectedRegion[];
  selected: number | null;
  tool: MaskTool;
  /** Grey out everything masked, so the user sees what will be compared. */
  preview: boolean;
  /** Set after a successful apply, for the confirmation line. */
  appliedTo: number | null;

  load: () => Promise<void>;
  selectTemplate: (templateId: string) => void;
  setTool: (tool: MaskTool) => void;
  togglePreview: () => void;
  select: (index: number | null) => void;
  toggleZone: (index: number) => void;
  moveZone: (index: number, rect: FracRect) => void;
  removeZone: (index: number) => void;
  addZone: (rect: FracRect) => void;
  addProtected: (rect: FracRect) => void;
  removeProtected: (index: number) => void;
  apply: () => Promise<void>;
  reset: () => void;
}

/** A rectangle with its corners in order and clamped to the sheet. */
export function normaliseRect(rect: FracRect): FracRect {
  const clamp = (value: number) => Math.min(Math.max(value, 0), 1);
  return {
    x0: clamp(Math.min(rect.x0, rect.x1)),
    y0: clamp(Math.min(rect.y0, rect.y1)),
    x1: clamp(Math.max(rect.x0, rect.x1)),
    y1: clamp(Math.max(rect.y0, rect.y1)),
  };
}

/** True for a rectangle too small to have been meant. */
export function isNegligible(rect: FracRect): boolean {
  return rect.x1 - rect.x0 < 0.005 || rect.y1 - rect.y0 < 0.005;
}

function templateById(templates: SheetTemplate[], id: string | null): SheetTemplate | null {
  if (id === null) return null;
  return templates.find((template) => template.template_id === id) ?? null;
}

export const useMaskStore = create<MaskState>((set, get) => ({
  loading: false,
  saving: false,
  errorMessage: null,
  templates: [],
  activeId: null,
  zones: [],
  protectedRegions: [],
  selected: null,
  tool: 'select',
  preview: false,
  appliedTo: null,

  load: async () => {
    set({ loading: true, errorMessage: null, appliedTo: null });
    try {
      const payload = await fetchMaskTemplates();
      const first = payload.templates[0] ?? null;
      set({
        loading: false,
        templates: payload.templates,
        activeId: first?.template_id ?? null,
        zones: first ? first.zones.map((zone) => ({ ...zone })) : [],
        protectedRegions: first ? first.protected.map((region) => ({ ...region })) : [],
        selected: null,
      });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : 'The sheet templates could not be read. Try again.';
      set({ loading: false, errorMessage: message });
    }
  },

  selectTemplate: (templateId) => {
    const template = templateById(get().templates, templateId);
    if (template === null) return;
    set({
      activeId: templateId,
      zones: template.zones.map((zone) => ({ ...zone })),
      protectedRegions: template.protected.map((region) => ({ ...region })),
      selected: null,
      appliedTo: null,
    });
  },

  setTool: (tool) => set({ tool }),
  togglePreview: () => set((state) => ({ preview: !state.preview })),
  select: (index) => set({ selected: index }),

  toggleZone: (index) =>
    set((state) => ({
      zones: state.zones.map((zone, position) =>
        position === index
          ? { ...zone, enabled: !zone.enabled, user_edited: true }
          : zone,
      ),
    })),

  moveZone: (index, rect) =>
    set((state) => ({
      zones: state.zones.map((zone, position) =>
        position === index
          ? { ...zone, rect: normaliseRect(rect), user_edited: true }
          : zone,
      ),
    })),

  removeZone: (index) =>
    set((state) => ({
      zones: state.zones.filter((_zone, position) => position !== index),
      selected: null,
    })),

  addZone: (rect) => {
    const bounds = normaliseRect(rect);
    if (isNegligible(bounds)) return;
    set((state) => ({
      zones: [
        ...state.zones,
        {
          type: USER_ZONE,
          rect: bounds,
          polygon: [],
          label: 'Exclusion zone',
          confidence: 1,
          evidence: ['Drawn by you in the mask editor.'],
          enabled: true,
          user_edited: true,
          needs_confirmation: false,
          ink_only: false,
          ink_threshold: 130,
          match_text: '',
        },
      ],
      selected: state.zones.length,
      tool: 'select',
    }));
  },

  addProtected: (rect) => {
    const bounds = normaliseRect(rect);
    if (isNegligible(bounds)) return;
    set((state) => ({
      protectedRegions: [
        ...state.protectedRegions,
        {
          type: 'user_protected',
          rect: bounds,
          label: 'Always compared',
          confidence: 1,
          evidence: ['Marked by you as always compared.'],
        },
      ],
      tool: 'select',
    }));
  },

  removeProtected: (index) =>
    set((state) => ({
      protectedRegions: state.protectedRegions.filter((_region, position) => position !== index),
    })),

  apply: async () => {
    const { activeId, zones, protectedRegions, templates } = get();
    if (activeId === null) return;
    set({ saving: true, errorMessage: null });
    try {
      const response = await saveMask(activeId, zones, protectedRegions);
      set({
        saving: false,
        appliedTo: response.applied_to,
        templates: templates.map((template) =>
          template.template_id === activeId
            ? { ...template, zones, protected: protectedRegions, confirmed: true }
            : template,
        ),
      });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : 'The mask could not be saved. Try again.';
      set({ saving: false, errorMessage: message });
    }
  },

  reset: () =>
    set({
      loading: false,
      saving: false,
      errorMessage: null,
      templates: [],
      activeId: null,
      zones: [],
      protectedRegions: [],
      selected: null,
      tool: 'select',
      preview: false,
      appliedTo: null,
    }),
}));

/** The template currently being edited, or null before anything loaded. */
export function activeTemplate(state: MaskState): SheetTemplate | null {
  return templateById(state.templates, state.activeId);
}

/** How many sheets the current edit will apply to, for the primary action. */
export function affectedSheets(state: MaskState): number {
  return activeTemplate(state)?.sheet_count ?? 0;
}

/** Zones the engine wants a human to confirm before they are used. */
export function proposedZones(state: MaskState): MaskZone[] {
  return state.zones.filter((zone) => zone.needs_confirmation);
}
