import { useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import { ProgressRail } from '../../components/ProgressRail';
import { StatusPill } from '../../components/StatusPill';
import type { StatusTone } from '../../components/StatusPill';
import type {
  ProgressEvent,
  RenameAction,
  RenameActionStatus,
} from '../../api/types';
import { baseName, truncateMiddle } from '../../lib/path';
import { useAppStore } from '../../store/appStore';
import {
  applyBlockReason,
  canApply,
  isProblemAction,
  renameCounts,
  summarySentence,
  useRenameStore,
  visibleActions,
} from '../../store/renameStore';
import type { RenameFilter } from '../../store/renameStore';

import './RenameScreen.css';

/** Tokens offered as chips, minus any already written into the template. */
const TEMPLATE_TOKENS = [
  'project',
  'originator',
  'volume',
  'level',
  'type',
  'role',
  'number',
  'rev',
  'title',
  'date',
  'status',
  'original',
  'extension',
];

/** Keystrokes settle for this long before a new dry run goes out. */
const PLAN_DEBOUNCE_MS = 250;

const ROW_HEIGHT = 44;
const PROBLEM_ROW_HEIGHT = 64;

/** How each planned-rename status reads and looks (status palette, not brand). */
const STATUS_META: Record<RenameActionStatus, { label: string; tone: StatusTone }> = {
  ok: { label: 'Will rename', tone: 'ok' },
  unchanged: { label: 'Unchanged', tone: 'neutral' },
  collision: { label: 'Collision', tone: 'danger' },
  invalid_name: { label: 'Invalid name', tone: 'danger' },
  path_too_long: { label: 'Path too long', tone: 'danger' },
  missing_tokens: { label: 'Missing tokens', tone: 'warn' },
  locked: { label: 'Locked', tone: 'warn' },
  reserved_name: { label: 'Reserved name', tone: 'danger' },
  source_missing: { label: 'Source missing', tone: 'warn' },
};

/**
 * Screen 4 — the rename review.
 *
 * The top section builds a naming template with a live preview of real file
 * names; the middle shows the full dry-run plan (problem rows first, inline
 * edits per row); the bottom bar applies it. Applying, cancelling and undoing
 * are polled from the engine — there is no progress socket for rename.
 */
export function RenameScreen() {
  const state = useRenameStore();
  const goToMatching = useAppStore((store) => store.goToMatching);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // On mount: fetch templates + context, reconcile any run left going.
  useEffect(() => {
    void state.init();
    return () => state.leave();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Every keystroke re-plans after a short pause, so typing never stacks
  // dry-run requests. The plan request itself is single-flight in the store.
  useEffect(() => {
    if (!state.ready || state.phase !== 'idle') return;
    const timer = window.setTimeout(() => {
      void state.planNow();
    }, PLAN_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    state.ready,
    state.phase,
    state.template,
    state.mode,
    state.useDisciplineFolders,
    state.overrides,
  ]);

  if (state.phase === 'running') return <RunningView goToMatching={goToMatching} />;
  if (state.phase === 'done' || state.phase === 'failed') {
    return <ResultView goToMatching={goToMatching} />;
  }
  if (confirmOpen) {
    return (
      <ConfirmApplyDialog
        goToMatching={goToMatching}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={(iUnderstand) => {
          setConfirmOpen(false);
          void state.apply(iUnderstand);
        }}
      />
    );
  }

  return (
    <div className="rename">
      <RenameHeader goToMatching={goToMatching} />
      <Messages />

      <TemplateBuilder />
      <PlanSection />
      <BottomBar onApply={() => setConfirmOpen(true)} />
    </div>
  );
}

// ── Shared bits ─────────────────────────────────────────────────────────

function RenameHeader({ goToMatching }: { goToMatching: () => void }) {
  const state = useRenameStore();
  const context = useMemo(() => {
    const parts: string[] = [];
    const known: [string, string | null | undefined][] = [
      ['project', state.context.project],
      ['originator', state.context.originator],
      ['status', state.context.status],
    ];
    for (const [key, value] of known) {
      if (value) parts.push(`${key} ${value}`);
    }
    return parts;
  }, [state.context.project, state.context.originator, state.context.status]);

  return (
    <header className="rename__head">
      <div className="rename__title-row">
        <button type="button" className="rename__back" onClick={goToMatching}>
          ← Matching
        </button>
        <h1>Rename the current issue</h1>
      </div>
      <p className="rename__sentence">
        {context.length > 0
          ? `From the sheet profile: ${context.join(' · ')}`
          : 'Plan the new file names below, then review the dry run before applying.'}
      </p>
    </header>
  );
}

/** Notices and engine errors, shared by every state of the screen. */
function Messages() {
  const state = useRenameStore();
  return (
    <>
      {state.notice && (
        <p className="rename__notice" role="status">
          {state.notice}
        </p>
      )}
      {(state.error || state.planError) && (
        <p className="rename__error" role="alert">
          {state.error ?? state.planError}
        </p>
      )}
    </>
  );
}

// ── Top: the template builder ───────────────────────────────────────────

function TemplateBuilder() {
  const state = useRenameStore();
  const templateRef = useRef<HTMLInputElement>(null);
  const template = state.template;

  // Tokens that are not already written into the template become chips.
  const chips = useMemo(() => {
    const present = new Set<string>();
    const pattern = /\{([a-z]+)(?::[a-z0-9]+)?\}/g;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(template)) !== null) {
      const token = match[1];
      if (token) present.add(token);
    }
    return TEMPLATE_TOKENS.filter((token) => !present.has(token));
  }, [template]);

  const insertToken = (token: string) => {
    const input = templateRef.current;
    const start = input?.selectionStart ?? template.length;
    const end = input?.selectionEnd ?? start;
    const tokenText = `{${token}}`;
    state.setTemplate(
      template.slice(0, start) + tokenText + template.slice(end),
    );
    const caret = start + tokenText.length;
    window.requestAnimationFrame(() => {
      input?.focus();
      input?.setSelectionRange(caret, caret);
    });
  };

  const activePreset = state.templates.find((item) => item.id === state.presetId) ?? null;

  return (
    <section className="rename__builder" aria-label="Naming template">
      <div className="rename__builder-row">
        <label className="rename__field" htmlFor="rename-preset">
          <span className="rename__field-label micro">Preset</span>
          <select
            id="rename-preset"
            className="rename__select"
            value={state.presetId ?? ''}
            onChange={(event) => state.setPreset(event.target.value)}
          >
            <option value="">Custom</option>
            {state.templates.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="rename__field rename__field--grow" htmlFor="rename-template">
          <span className="rename__field-label micro">Template</span>
          <span className="rename__template-wrap">
            <input
              id="rename-template"
              ref={templateRef}
              className="rename__template-input tabular"
              value={template}
              spellCheck={false}
              placeholder="e.g. {project}-{originator}-{number}_{rev}"
              onChange={(event) => state.setTemplate(event.target.value)}
            />
            {state.planning && (
              <span className="rename__planning micro" role="status">
                Updating…
              </span>
            )}
          </span>
        </label>
      </div>

      <div className="rename__tokens" role="group" aria-label="Insert a token">
        {chips.length === 0 ? (
          <span className="rename__tokens-empty micro">
            Every token is already in the template.
          </span>
        ) : (
          chips.map((token) => (
            <button
              key={token}
              type="button"
              className="rename__token"
              onClick={() => insertToken(token)}
              title={`Insert {${token}} at the cursor`}
            >
              {'{'}
              {token}
              {'}'}
            </button>
          ))
        )}
      </div>

      {activePreset?.description && (
        <p className="rename__desc micro">{activePreset.description}</p>
      )}

      <div className="rename__preview">
        <span className="rename__preview-label micro">Preview with real file names</span>
        {state.plan && state.plan.preview.length > 0 ? (
          <ul className="rename__preview-list">
            {state.plan.preview.map((name) => (
              <li key={name} className="rename__preview-name tabular">
                {name}
              </li>
            ))}
          </ul>
        ) : (
          <p className="rename__preview-empty micro">
            Type a template to see what real files will be called.
          </p>
        )}
      </div>

      <div className="rename__choices">
        <div className="rename__mode" role="radiogroup" aria-label="Where the renamed files go">
          <label className={`rename__choice${state.mode === 'copy' ? ' rename__choice--on' : ''}`}>
            <input
              type="radio"
              name="rename-mode"
              checked={state.mode === 'copy'}
              onChange={() => state.setMode('copy')}
            />
            <span>Copy renamed files to the output folder</span>
          </label>
          <label
            className={`rename__choice${state.mode === 'in_place' ? ' rename__choice--on' : ''}`}
          >
            <input
              type="radio"
              name="rename-mode"
              checked={state.mode === 'in_place'}
              onChange={() => state.setMode('in_place')}
            />
            <span>Rename files in place</span>
          </label>
        </div>
        <label className="rename__choice rename__folders">
          <input
            type="checkbox"
            checked={state.useDisciplineFolders}
            disabled={state.mode === 'in_place'}
            title={
              state.mode === 'in_place'
                ? 'Discipline folders apply to the copied output set'
                : undefined
            }
            onChange={(event) => state.setFolders(event.target.checked)}
          />
          <span>Sort into discipline folders</span>
        </label>
      </div>

      {state.mode === 'in_place' && (
        <p className="rename__mode-warn">
          Renames the files where they are. Original file names are replaced.
        </p>
      )}
    </section>
  );
}

// ── Middle: the plan table ──────────────────────────────────────────────

function PlanSection() {
  const state = useRenameStore();
  const counts = renameCounts(state);
  const summary = state.plan?.summary;
  const total = counts.total;

  const filters: { key: RenameFilter; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: total },
    { key: 'ok', label: 'OK', count: summary?.ok ?? 0 },
    { key: 'problems', label: 'Problems', count: counts.attention },
    { key: 'unchanged', label: 'Unchanged', count: summary?.unchanged ?? 0 },
  ];

  return (
    <section className="rename__plan">
      <header className="rename__plan-head">
        <h2 className="rename__section-title">
          Plan <span className="rename__section-count micro tabular">({total})</span>
        </h2>
        <div className="rename__filters" role="group" aria-label="Filter the plan">
          {filters.map((filter) => (
            <button
              key={filter.key}
              type="button"
              className={`rename__filter${state.filter === filter.key ? ' rename__filter--on' : ''}`}
              aria-pressed={state.filter === filter.key}
              onClick={() => state.setFilter(filter.key)}
            >
              {filter.label} <span className="tabular">{filter.count}</span>
            </button>
          ))}
        </div>
      </header>

      <PlanTable />
    </section>
  );
}

function PlanTable() {
  const state = useRenameStore();
  const rows = visibleActions(state);
  const [editing, setEditing] = useState<{ source: string; value: string } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => {
      const action = rows[index];
      return action && isProblemAction(action) ? PROBLEM_ROW_HEIGHT : ROW_HEIGHT;
    },
    overscan: 12,
  });

  // A new plan (after an inline edit) or a filter change closes the editor.
  useEffect(() => {
    setEditing(null);
  }, [state.plan, state.filter]);

  const beginEdit = (action: RenameAction) => {
    setEditing({ source: action.source_path, value: action.new_name });
  };

  const commitEdit = () => {
    const current = editing;
    if (!current) return;
    setEditing(null);
    const action = state.plan?.actions.find(
      (candidate) => candidate.source_path === current.source,
    );
    if (action && action.new_name === current.value.trim()) return;
    state.setOverride(current.source, current.value);
  };

  if (rows.length === 0) {
    return (
      <div className="rename__table">
        <p className="rename__empty">
          {state.plan === null
            ? 'No plan yet — check the template above.'
            : state.filter === 'all'
              ? 'The current issue has no drawings to rename.'
              : 'No rows match this filter.'}
        </p>
      </div>
    );
  }

  return (
    <div className="rename__table">
      <div className="rename__table-cols micro">
        <span>Current name</span>
        <span className="rename__seam-gap" aria-hidden="true" />
        <span>New name</span>
        <span className="rename__table-status">Status</span>
      </div>
      <div className="rename__table-scroll" ref={scrollRef}>
        <div
          className="rename__table-canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const action = rows[virtualRow.index];
            if (!action) return null;
            const problem = isProblemAction(action);
            const isEditing = editing?.source === action.source_path;
            const hasOverride = Object.prototype.hasOwnProperty.call(
              state.overrides,
              action.source_path,
            );
            const meta = STATUS_META[action.status];
            const warnings = action.warnings.length > 0 ? action.warnings.join(' ') : null;

            return (
              <div
                key={action.source_path}
                className={`rename__plan-row${problem ? ' rename__plan-row--problem' : ''}`}
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
              >
                <div className="rename__row-grid">
                  <span className="rename__name tabular" title={action.source_path}>
                    {action.old_name}
                  </span>
                  <span className="rename__arrow" aria-hidden="true">
                    →
                  </span>
                  {isEditing ? (
                    <EditNameInput
                      value={editing.value}
                      onCommit={() => commitEdit()}
                      onCancel={() => setEditing(null)}
                      onChange={(value) =>
                        setEditing((current) =>
                          current ? { ...current, value } : current,
                        )
                      }
                    />
                  ) : (
                    <button
                      type="button"
                      className={`rename__name rename__name--new tabular${
                        hasOverride ? ' rename__name--edited' : ''
                      }`}
                      title={
                        hasOverride
                          ? 'Name changed by you — click to edit again'
                          : 'Click to change this file name'
                      }
                      onClick={() => beginEdit(action)}
                    >
                      {action.new_name || '—'}
                    </button>
                  )}
                  <span className="rename__status">
                    <StatusPill tone={meta.tone} title={warnings ?? undefined}>
                      {meta.label}
                    </StatusPill>
                  </span>
                </div>
                {problem && warnings && (
                  <p className="rename__warning" title={action.warnings.join('\n')}>
                    {warnings}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EditNameInput({
  value,
  onChange,
  onCommit,
  onCancel,
}: {
  value: string;
  onChange: (value: string) => void;
  onCommit: () => void;
  onCancel: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const done = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const commitOnce = () => {
    if (done.current) return;
    done.current = true;
    onCommit();
  };

  return (
    <input
      ref={inputRef}
      type="text"
      className="rename__name-input tabular"
      value={value}
      spellCheck={false}
      aria-label="New file name"
      onChange={(event) => onChange(event.target.value)}
      onBlur={commitOnce}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          commitOnce();
        } else if (event.key === 'Escape') {
          if (done.current) return;
          done.current = true;
          onCancel();
        }
      }}
    />
  );
}

// ── Bottom bar ──────────────────────────────────────────────────────────

function BottomBar({ onApply }: { onApply: () => void }) {
  const state = useRenameStore();
  const ready = canApply(state);
  const blockReason = applyBlockReason(state);

  return (
    <div className="rename__bar">
      <p className="rename__bar-sentence" role="status">
        {summarySentence(state)}
      </p>
      <div className="rename__bar-actions">
        <button
          type="button"
          className="button button--primary rename__apply"
          onClick={onApply}
          disabled={!ready}
          title={blockReason ?? undefined}
        >
          Apply renames
        </button>
      </div>
    </div>
  );
}

// ── The apply confirmation (full-screen, replaces content) ──────────────

function ConfirmApplyDialog({
  goToMatching,
  onCancel,
  onConfirm,
}: {
  goToMatching: () => void;
  onCancel: () => void;
  onConfirm: (iUnderstand: boolean) => void;
}) {
  const state = useRenameStore();
  const [understood, setUnderstood] = useState(false);
  const plan = state.plan;
  const counts = renameCounts(state);
  const inPlace = state.mode === 'in_place';

  const folderName = plan ? baseName(plan.output_dir) : '';
  const plural = counts.toChange === 1 ? 'file' : 'files';
  const sentence = inPlace
    ? `${counts.toChange} ${plural} will be renamed in place with new names. The original file names are replaced.`
    : folderName
      ? `${counts.toChange} ${plural} will be copied into ${folderName} with new names. Your original files will not be changed.`
      : `${counts.toChange} ${plural} will be copied into the output folder with new names. Your original files will not be changed.`;

  return (
    <div className="rename-dialog">
      <div className="rename-dialog__panel" role="dialog" aria-modal="true" aria-label="Apply renames">
        <button type="button" className="rename__back" onClick={goToMatching}>
          ← Matching
        </button>
        <h1 className="rename-dialog__title">Apply renames</h1>

        {state.notice && (
          <p className="rename__notice" role="status">
            {state.notice}
          </p>
        )}
        {state.error && (
          <p className="rename__error" role="alert">
            {state.error}
          </p>
        )}

        <p className="rename-dialog__sentence">{sentence}</p>

        {inPlace && (
          <p className="rename-dialog__warn">
            Renames the files where they are. Original file names are replaced.
          </p>
        )}

        <p className="rename-dialog__facts micro">
          <span className="tabular">{counts.unchanged}</span> unchanged ·{' '}
          <span className="tabular">{counts.attention}</span>{' '}
          {counts.attention === 1 ? 'row' : 'rows'} skipped or need attention
        </p>

        {plan && plan.errors.length > 0 && (
          <p className="rename__error" role="alert">
            {plan.errors.join(' ')}
          </p>
        )}

        <div className="rename-dialog__actions">
          <button type="button" className="button" onClick={onCancel}>
            Cancel
          </button>
          {inPlace && (
            <label className="rename-dialog__understand">
              <input
                type="checkbox"
                checked={understood}
                onChange={(event) => setUnderstood(event.target.checked)}
              />
              <span>
                I understand — keep copies of anything you need before renaming.
              </span>
            </label>
          )}
          <button
            type="button"
            className="button button--primary"
            disabled={inPlace && !understood}
            onClick={() => onConfirm(inPlace ? understood : true)}
          >
            Apply renames
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Running ─────────────────────────────────────────────────────────────

function RunningView({ goToMatching }: { goToMatching: () => void }) {
  const state = useRenameStore();

  const event: ProgressEvent | null = useMemo(() => {
    const total = state.progress.total;
    const item =
      state.progress.current_item ?? state.message ?? (state.kind === 'undo' ? 'Undoing…' : 'Preparing…');
    return {
      run_id: state.runId ?? '',
      stage: 'reconcile',
      kind: 'progress',
      current: state.progress.current,
      total,
      current_item: item,
      elapsed: 0,
      message: state.message ?? '',
      fraction: total > 0 ? state.progress.current / total : 0,
      eta: null,
      detail: {},
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.runId, state.kind, state.progress, state.message]);

  const note =
    state.kind === 'undo'
      ? 'Undoing the last rename, verifying each file before it is reversed.'
      : state.mode === 'in_place'
        ? 'Renaming each file in place and logging it for undo.'
        : 'Copying each drawing into the output folder and checking it landed correctly.';

  return (
    <div className="rename">
      <RenameHeader goToMatching={goToMatching} />
      <Messages />
      <div className="rename__run-panel">
        <ProgressRail event={event} onCancel={() => void state.cancel()} />
        <p className="rename__running-note micro">{note}</p>
      </div>
    </div>
  );
}

// ── The result panel (after apply or a failure) ─────────────────────────

function ResultView({ goToMatching }: { goToMatching: () => void }) {
  const state = useRenameStore();
  const failed = state.phase === 'failed';
  const wasUndo = state.kind === 'undo';
  const plan = state.plan;
  const counts = renameCounts(state);
  const inPlace = state.mode === 'in_place';
  const verbatim = failed ? state.error ?? state.message : state.message;

  return (
    <div className="rename">
      <RenameHeader goToMatching={goToMatching} />
      <Messages />

      <div className="rename__result">
        <h2 className="rename__result-title">
          {failed
            ? wasUndo
              ? 'The undo could not finish'
              : 'The rename could not finish'
            : wasUndo
              ? 'Undo finished'
              : 'Renaming finished'}
        </h2>

        {verbatim && (
          <p className="rename__result-message" role={failed ? 'alert' : 'status'}>
            {verbatim}
          </p>
        )}

        <div className="rename__result-counts tabular">
          <div className="rename__result-count">
            <span className="rename__result-number">{state.completed}</span>
            <span className="micro">
              {wasUndo ? 'reversed' : inPlace ? 'renamed in place' : 'copied'}
            </span>
          </div>
          <div className={`rename__result-count${state.failed > 0 ? ' rename__result-count--failed' : ''}`}>
            <span className="rename__result-number">{state.failed}</span>
            <span className="micro">failed</span>
          </div>
        </div>

        {state.failedItems.length > 0 && (
          <div className="rename__failures">
            {state.failedItems.slice(0, 5).map((item) => (
              <p key={item.path} className="rename__failure micro" title={item.path}>
                <span className="rename__failure-path tabular">{baseName(item.path)}</span>
                <span className="rename__failure-reason">
                  {item.reason ?? item.error ?? item.note}
                </span>
              </p>
            ))}
            {state.failedItems.length > 5 && (
              <p className="rename__failure-more micro">
                …and {state.failedItems.length - 5} more, listed in the log file.
              </p>
            )}
          </div>
        )}

        {!failed && !wasUndo && plan && plan.output_dir && !inPlace && (
          <PathRow label="New files are in" path={plan.output_dir} />
        )}
        {!failed && !wasUndo && inPlace && (
          <p className="rename__result-note micro">
            Your files were renamed where they are. The undo log can reverse
            this run.
          </p>
        )}
        {!failed && state.undoLogPath && (
          <PathRow label="Undo log" path={state.undoLogPath} />
        )}

        <div className="rename__result-actions">
          <button
            type="button"
            className="button button--primary"
            onClick={() => void state.undo()}
            disabled={!state.undoEnabled}
            title={
              state.undoEnabled
                ? undefined
                : 'Nothing to undo — the last run did not rename anything.'
            }
          >
            Undo all renames
          </button>
          <button type="button" className="button" onClick={() => state.backToPlan()}>
            Back to the plan
          </button>
        </div>

        {counts.total > 0 && (
          <p className="rename__result-sub micro">
            {counts.total} files in the plan · {counts.attention}{' '}
            {counts.attention === 1 ? 'row' : 'rows'} skipped
          </p>
        )}
      </div>
    </div>
  );
}

/** A copyable text row, not a link: full path in title, copy on demand. */
function PathRow({ label, path }: { label: string; path: string }) {
  const setNotice = useRenameStore((store) => store.setNotice);

  const copy = async () => {
    if (!path) return;
    try {
      await navigator.clipboard.writeText(path);
      setNotice(`${label} copied to the clipboard.`);
    } catch {
      // Clipboard may be unavailable in the shell; the text is selectable.
    }
  };

  return (
    <div className="rename__path-row">
      <span className="rename__path-label micro">{label}</span>
      <span className="rename__path tabular" title={path}>
        {truncateMiddle(path)}
      </span>
      <button type="button" className="rename__path-copy micro" onClick={() => void copy()}>
        Copy
      </button>
    </div>
  );
}
