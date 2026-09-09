import { useEffect, useMemo, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import { ProgressRail } from '../../components/ProgressRail';
import { StatusPill } from '../../components/StatusPill';
import type { StatusTone } from '../../components/StatusPill';
import type { ProgressEvent, SheetBrief } from '../../api/types';
import type { MatchPair } from '../../api/types';
import { useAppStore } from '../../store/appStore';
import {
  autoPairs,
  canContinue,
  candidatesForThreshold,
  countByStatus,
  decisionFor,
  reviewPairs,
  useMatchingStore,
} from '../../store/matchingStore';
import type { ManualPair } from '../../store/matchingStore';

import './MatchingScreen.css';

const REVIEW_ROW_HEIGHT = 96;
const AUTO_ROW_HEIGHT = 40;
const UNMATCHED_ROW_HEIGHT = 46;

/**
 * Screen 3 — matching review.
 *
 * The engine proposes pairs between the two issues. Pairs it is sure about
 * sit collapsed under "Auto-matched"; the ones needing a human decision are
 * the focus of the screen, and are keyboard-first: J/K move, A/R decide,
 * Enter offers a different match. Decisions stay local until "Save review".
 */
export function MatchingScreen() {
  const state = useMatchingStore();
  const goToRegister = useAppStore((store) => store.goToRegister);

  useEffect(() => {
    void state.enter();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A new run must never sit behind a stale result: while the engine is
  // running (or after a failure) the screen shows its progress instead.
  if (
    state.result === null ||
    state.status === 'running' ||
    state.status === 'failed'
  ) {
    return <RunningView goToRegister={goToRegister} />;
  }

  return (
    <div className="matching">
      <MatchingHeader goToRegister={goToRegister} />
      <Messages />

      <div className="matching__body">
        <AutoSection />
        <ReviewSection />
        <UnmatchedSection />
      </div>

      <BottomBar />
    </div>
  );
}

function MatchingHeader({ goToRegister }: { goToRegister: () => void }) {
  const state = useMatchingStore();
  const counts = countByStatus(state);
  const unresolved = counts.unresolved;

  return (
    <header className="matching__head">
      <div className="matching__title-row">
        <button type="button" className="matching__back" onClick={goToRegister}>
          ← Register
        </button>
        <h1>Match drawings</h1>
      </div>
      <p className="matching__sentence" role="status">
        {counts.auto} auto-matched · {unresolved}{' '}
        {unresolved === 1 ? 'needs' : 'need'} review · {counts.unmatched} unmatched
      </p>
      {state.result && state.result.notes.length > 0 && (
        <ul className="matching__notes">
          {state.result.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </header>
  );
}

/** Notices and engine errors, shared by every state of the screen. */
function Messages() {
  const state = useMatchingStore();
  return (
    <>
      {state.notice && (
        <p className="matching__notice" role="status">
          {state.notice}
        </p>
      )}
      {state.error && (
        <p className="matching__error" role="alert">
          {state.error}
        </p>
      )}
    </>
  );
}

/** A live rail while the run is going; a failure panel once it is over. */
function RunningView({ goToRegister }: { goToRegister: () => void }) {
  const state = useMatchingStore();

  const event: ProgressEvent | null = useMemo(() => {
    if (state.status !== 'running') return null;
    const total = state.total;
    const message = state.message ?? 'Matching drawings…';
    return {
      run_id: state.runId ?? '',
      stage: 'reconcile',
      kind: 'progress',
      current: state.current,
      total,
      current_item: message,
      elapsed: 0,
      message,
      fraction: total > 0 ? state.current / total : 0,
      eta: null,
      detail: {},
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.status, state.runId, state.current, state.total, state.message]);

  const failed = state.status === 'failed';
  const blocked = failed || state.status === 'ready';
  // Engine messages are shown verbatim; only the failure summary is ours.
  const text = blocked
    ? (state.error ?? state.message ?? 'The match run failed.')
    : null;

  return (
    <div className="matching matching--run">
      <div className="matching__title-row">
        <button type="button" className="matching__back" onClick={goToRegister}>
          ← Register
        </button>
        <h1>Match drawings</h1>
      </div>
      {state.notice && (
        <p className="matching__notice" role="status">
          {state.notice}
        </p>
      )}

      {blocked ? (
        <div className="matching__failed" role="alert">
          <p>{text}</p>
          <button
            type="button"
            className="button button--primary"
            onClick={() => void state.retry()}
          >
            Try again
          </button>
        </div>
      ) : (
        <>
          {state.error && (
            <p className="matching__error" role="alert">
              {state.error}
            </p>
          )}
          <ProgressRail event={event} />
          <p className="matching__running-note micro">
            Matching compares the register's drawings across both issues. This
            usually takes less than a minute.
          </p>
        </>
      )}
    </div>
  );
}

// ── Section 1: auto-matched ────────────────────────────────────────────

function AutoSection() {
  const state = useMatchingStore();
  const pairs = autoPairs(state);

  return (
    <section className="matching__section matching__section--auto">
      <header className="matching__section-head">
        <h2 className="matching__section-title">Auto-matched</h2>
        <span className="matching__section-count micro">
          <span className="tabular">{pairs.length}</span>{' '}
          {pairs.length === 1 ? 'pair' : 'pairs'}
        </span>
        {pairs.length > 0 && (
          <button
            type="button"
            className="matching__toggle"
            aria-expanded={state.expandedAuto}
            onClick={() => state.toggleAuto()}
          >
            {state.expandedAuto ? 'Hide' : 'Review'}
          </button>
        )}
      </header>

      {state.expandedAuto && pairs.length > 0 && <AutoList pairs={pairs} />}
    </section>
  );
}

function AutoList({ pairs }: { pairs: MatchPair[] }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: pairs.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => AUTO_ROW_HEIGHT,
    overscan: 12,
  });

  return (
    <div className="matching__auto-panel">
      <div className="matching__auto-cols micro">
        <span>Previous issue</span>
        <span className="matching__seam-cell" aria-hidden="true" />
        <span>Current issue</span>
        <span className="matching__auto-conf-col">Match</span>
      </div>
      <div className="matching__auto-scroll" ref={parentRef}>
        <div
          className="matching__auto-canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const pair = pairs[virtualRow.index];
            if (!pair) return null;
            return (
              <div
                key={pair.old.key}
                className="matching__auto-row"
                title={pair.reason}
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
              >
                <SheetInline sheet={pair.old} />
                <span className="matching__seam-cell" aria-hidden="true" />
                <SheetInline sheet={pair.new} />
                <Confidence value={pair.confidence} />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Section 2: needs review ────────────────────────────────────────────

function ReviewSection() {
  const state = useMatchingStore();
  const pairs = reviewPairs(state);
  const counts = countByStatus(state);
  const candidates = candidatesForThreshold(state, state.threshold);

  if (pairs.length === 0) {
    return (
      <section className="matching__section matching__section--review">
        <header className="matching__section-head">
          <h2 className="matching__section-title">Needs review</h2>
          <span className="matching__section-count micro">0 pairs</span>
        </header>
        <p className="matching__empty">Nothing needs a decision — every pair was matched.</p>
      </section>
    );
  }

  return (
    <section className="matching__section matching__section--review">
      <header className="matching__section-head">
        <h2 className="matching__section-title">Needs review</h2>
        <span className="matching__section-count micro">
          <span className="tabular">{counts.unresolved}</span> of{' '}
          <span className="tabular">{pairs.length}</span> left to decide
        </span>
        <span className="matching__hint micro">
          J next · K previous · A accept · R reject · Enter alternatives
        </span>
      </header>

      <div className="matching__bulk">
        <label className="matching__bulk-label" htmlFor="matching-threshold">
          Accept all above{' '}
          <span className="tabular">{formatConfidence(state.threshold)}</span>
        </label>
        <input
          id="matching-threshold"
          type="range"
          min={0.5}
          max={1}
          step={0.01}
          className="matching__slider"
          value={state.threshold}
          onChange={(event) => state.setThreshold(Number(event.target.value))}
        />
        <span className="matching__bulk-count micro tabular">
          {candidates.length} {candidates.length === 1 ? 'row' : 'rows'}
        </span>
        <button
          type="button"
          className="matching__apply"
          onClick={() => state.bulkAccept(state.threshold)}
          disabled={candidates.length === 0}
        >
          Accept all
        </button>
      </div>

      <ReviewList pairs={pairs} />
    </section>
  );
}

function ReviewList({ pairs }: { pairs: MatchPair[] }) {
  const state = useMatchingStore();
  const scrollRef = useRef<HTMLDivElement>(null);
  const selectRefs = useRef(new Map<string, HTMLSelectElement>());
  const selected = state.selectedReviewIndex;

  const virtualizer = useVirtualizer({
    count: pairs.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => REVIEW_ROW_HEIGHT,
    overscan: 8,
  });

  const keepFocus = () => scrollRef.current?.focus();

  // The review list is the focus of the screen: claim keyboard focus when it
  // first appears so J/K/A/R work straight away.
  useEffect(() => {
    scrollRef.current?.focus();
  }, []);

  // Focus is what makes the keys work, so the current row stays in view.
  useEffect(() => {
    if (selected === null) return;
    virtualizer.scrollToIndex(selected, { align: 'auto' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, pairs.length]);

  const selectedPair = selected === null ? null : (pairs[selected] ?? null);

  const openAlternatives = (pair: MatchPair) => {
    const element = selectRefs.current.get(pair.old.key);
    if (!element) return;
    if (typeof HTMLSelectElement.prototype.showPicker === 'function') {
      try {
        element.showPicker();
        return;
      } catch {
        // showPicker needs a user gesture; the keydown below is one, but if
        // the browser disagrees, plain focus is still a working fallback.
      }
    }
    element.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    const typing = target.closest('input, select');
    const clicking = target.closest('button');

    if (event.key === 'Enter') {
      if (typing || clicking) return;
      event.preventDefault();
      if (selectedPair?.ambiguous) openAlternatives(selectedPair);
      return;
    }

    if (typing) return;
    const pair = selectedPair;
    if (!pair) return;

    if (event.key === 'j') {
      event.preventDefault();
      state.moveReviewSelection(1);
    } else if (event.key === 'k') {
      event.preventDefault();
      state.moveReviewSelection(-1);
    } else if (event.key === 'a') {
      event.preventDefault();
      state.accept(pair.old.key);
      keepFocus();
    } else if (event.key === 'r') {
      event.preventDefault();
      state.reject(pair.old.key);
      keepFocus();
    }
  };

  const registerSelect = (key: string, element: HTMLSelectElement | null) => {
    if (element) selectRefs.current.set(key, element);
    else selectRefs.current.delete(key);
  };

  return (
    <div className="matching__review-panel">
      <div className="matching__review-cols micro">
        <span>Previous issue</span>
        <span className="matching__seam-cell" aria-hidden="true" />
        <span>Current issue</span>
      </div>
      <div
        className="matching__review-scroll"
        ref={scrollRef}
        tabIndex={0}
        aria-label="Pairs needing review"
        onKeyDown={onKeyDown}
      >
        <div
          className="matching__review-canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const pair = pairs[virtualRow.index];
            if (!pair) return null;
            const index = virtualRow.index;
            const decision = decisionFor(state, pair);
            const isSelected = selected === index;
            const decisionOn = (key: string, next: 'accepted' | 'rejected') => {
              if (next === 'accepted') state.accept(key);
              else state.reject(key);
              keepFocus();
            };
            return (
              <div
                key={pair.old.key}
                className={`matching__review-row${isSelected ? ' matching__review-row--selected' : ''}`}
                aria-current={isSelected ? 'true' : undefined}
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
                onClick={(event) => {
                  const clicked = event.target as HTMLElement;
                  if (clicked.closest('button, select')) return;
                  state.selectReview(isSelected ? null : index);
                }}
              >
                <div className="matching__pair">
                  <SheetCell sheet={pair.old} />
                  <span className="matching__seam-cell" aria-hidden="true" />
                  <SheetCell sheet={pair.new} />
                </div>

                <div className="matching__pair-meta">
                  <span className="matching__reason" title={pair.reason}>
                    {pair.reason}
                  </span>

                  {pair.ambiguous && (
                    <label className="matching__alternative">
                      <span aria-hidden="true">⚠</span>
                      <span className="matching__alternative-label">Different match</span>
                      <select
                        className="matching__alternative-select"
                        value={
                          decision === 'manual'
                            ? (
                                state.manual.find(
                                  (item: ManualPair) => item.old_key === pair.old.key,
                                )?.new_key ?? ''
                              )
                            : ''
                        }
                        ref={(element) => registerSelect(pair.old.key, element)}
                        aria-label={`Choose a different match for ${pair.old.filename}`}
                        onChange={(event) =>
                          state.chooseAlternative(pair.old.key, event.target.value || null)
                        }
                      >
                        <option value="">Keep the original match</option>
                        {pair.alternatives.map((alternative) => (
                          <option key={alternative.sheet.key} value={alternative.sheet.key}>
                            {alternative.sheet.filename} · {formatConfidence(alternative.confidence)}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}

                  <Confidence value={pair.confidence} />

                  {decision === null ? (
                    <span className="matching__decide">
                      <button
                        type="button"
                        className="matching__decide-accept"
                        onClick={() => decisionOn(pair.old.key, 'accepted')}
                      >
                        Accept
                      </button>
                      <button
                        type="button"
                        className="matching__decide-reject"
                        onClick={() => decisionOn(pair.old.key, 'rejected')}
                      >
                        Reject
                      </button>
                    </span>
                  ) : (
                    <span className="matching__decision">
                      {decision === 'accepted' && <StatusPill tone="ok">Accepted</StatusPill>}
                      {decision === 'rejected' && <StatusPill tone="danger">Rejected</StatusPill>}
                      {decision === 'manual' && <StatusPill tone="info">Manual</StatusPill>}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Section 3: unmatched ───────────────────────────────────────────────

function UnmatchedSection() {
  const state = useMatchingStore();
  const oldSheets = state.result?.old_unmatched ?? [];
  const newSheets = state.result?.new_unmatched ?? [];
  const oldKey = state.manualOldKey;
  const newKey = state.manualNewKey;
  const oldSheet = oldSheets.find((sheet) => sheet.key === oldKey) ?? null;
  const newSheet = newSheets.find((sheet) => sheet.key === newKey) ?? null;
  const canPair = Boolean(oldSheet && newSheet);
  const alreadyPaired = state.manual.some(
    (item) => oldKey !== null && item.old_key === oldKey && item.new_key === newKey,
  );

  const both = oldSheets.length + newSheets.length;
  if (both === 0) return null;

  return (
    <section className="matching__section matching__section--unmatched">
      <header className="matching__section-head">
        <h2 className="matching__section-title">Unmatched</h2>
        <span className="matching__section-count micro">
          <span className="tabular">{oldSheets.length}</span> in the previous issue only ·{' '}
          <span className="tabular">{newSheets.length}</span> in the current issue only
        </span>
      </header>

      <div className="matching__unmatched-panels">
        <UnmatchedPane
          label={`Only in previous issue (${oldSheets.length})`}
          sheets={oldSheets}
          selectedKey={oldKey}
          pairedKeys={pairedKeys(state.manual, 'old')}
          onSelect={(key) => state.setManualOld(state.manualOldKey === key ? null : key)}
        />
        <UnmatchedPane
          label={`Only in current issue (${newSheets.length})`}
          sheets={newSheets}
          selectedKey={newKey}
          pairedKeys={pairedKeys(state.manual, 'new')}
          onSelect={(key) => state.setManualNew(state.manualNewKey === key ? null : key)}
        />
      </div>

      <div className="matching__pair-bar">
        <p className="matching__pair-status micro">
          {oldSheet && newSheet ? (
            <>
              Pairing <strong>{oldSheet.filename}</strong> with{' '}
              <strong>{newSheet.filename}</strong>
            </>
          ) : (
            'Choose one sheet on each side to pair them by hand.'
          )}
        </p>
        <button
          type="button"
          className="button matching__pair-button"
          disabled={!canPair}
          onClick={() => {
            if (oldKey !== null && newKey !== null) state.pairManually(oldKey, newKey);
          }}
        >
          {alreadyPaired ? 'Unpair' : 'Pair manually'}
        </button>
      </div>
    </section>
  );
}

/** Which sheet keys already have a manual decision, on one side. */
function pairedKeys(manual: ManualPair[], side: 'old' | 'new'): Set<string> {
  const keys = new Set<string>();
  for (const item of manual) keys.add(side === 'old' ? item.old_key : item.new_key);
  return keys;
}

function UnmatchedPane({
  label,
  sheets,
  selectedKey,
  pairedKeys: paired,
  onSelect,
}: {
  label: string;
  sheets: SheetBrief[];
  selectedKey: string | null;
  pairedKeys: Set<string>;
  onSelect: (key: string) => void;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: sheets.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => UNMATCHED_ROW_HEIGHT,
    overscan: 8,
  });

  return (
    <div className="matching__unmatched-pane">
      <h3 className="matching__unmatched-title micro">{label}</h3>
      <div className="matching__unmatched-scroll" ref={parentRef}>
        <div
          className="matching__unmatched-canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const sheet = sheets[virtualRow.index];
            if (!sheet) return null;
            const isSelected = selectedKey === sheet.key;
            const isPaired = paired.has(sheet.key);
            return (
              <button
                key={sheet.key}
                type="button"
                className={`matching__unmatched-row${
                  isSelected ? ' matching__unmatched-row--selected' : ''
                }`}
                aria-pressed={isSelected}
                title={
                  isPaired
                    ? 'Already paired by hand — pick it and the other side to unpair'
                    : sheet.abs_path
                }
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
                onClick={() => onSelect(sheet.key)}
              >
                {isPaired && (
                  <span className="matching__paired" aria-hidden="true">
                    ✓
                  </span>
                )}
                <span className="matching__file">{sheet.filename}</span>
                <span className="matching__file-meta micro">
                  <span className="tabular">{sheet.drawing_no ?? '—'}</span>
                  {sheet.title ? ` · ${sheet.title}` : ''}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Bottom bar ─────────────────────────────────────────────────────────

function BottomBar() {
  const state = useMatchingStore();
  const goToRename = useAppStore((store) => store.goToRename);
  const counts = countByStatus(state);
  const ready = canContinue(state);
  const unresolved = counts.unresolved;
  const nothingSaved = state.accepted.length + state.rejected.length + state.manual.length === 0;

  return (
    <div className="matching__bar">
      <p className="matching__bar-sentence" role="status">
        {counts.auto} auto-matched · {unresolved} {unresolved === 1 ? 'needs' : 'need'} review ·{' '}
        {counts.unmatched} unmatched
      </p>
      <div className="matching__bar-actions">
        <button
          type="button"
          className="button matching__save"
          onClick={() => void state.save()}
          disabled={state.saving || nothingSaved}
          title={nothingSaved ? 'Decide at least one pair before saving' : undefined}
        >
          {state.saving ? 'Saving…' : 'Save review'}
        </button>
        <button
          type="button"
          className="button button--primary"
          onClick={goToRename}
          disabled={!ready}
          title={
            ready
              ? undefined
              : unresolved > 0
                ? `${unresolved} ${unresolved === 1 ? 'pair still needs' : 'pairs still need'} a decision before renaming`
                : 'Save the review before continuing to rename'
          }
        >
          Continue to rename →
        </button>
      </div>
    </div>
  );
}

// ── Small building blocks ──────────────────────────────────────────────

/** One side of a review pair: name plus the drawing number and title. */
function SheetCell({ sheet }: { sheet: SheetBrief }) {
  return (
    <div className="matching__sheet">
      <span className="matching__file" title={sheet.abs_path}>
        {sheet.filename}
      </span>
      <span className="matching__file-meta micro">
        <span className="tabular">{sheet.drawing_no ?? '—'}</span>
        {sheet.title ? ` · ${sheet.title}` : ''}
        {sheet.revision ? ` · rev ${sheet.revision}` : ''}
      </span>
    </div>
  );
}

/** A one-line side for the auto list, where two lines would be too tall. */
function SheetInline({ sheet }: { sheet: SheetBrief }) {
  return (
    <span className="matching__inline" title={sheet.abs_path}>
      <span className="matching__file">{sheet.filename}</span>
      <span className="matching__file-meta micro">
        <span className="tabular">{sheet.drawing_no ?? '—'}</span>
        {sheet.revision ? ` · rev ${sheet.revision}` : ''}
      </span>
    </span>
  );
}

function Confidence({ value }: { value: number }) {
  const tone = confidenceTone(value);
  const percent = Math.round(value * 100);
  return (
    <span
      className="matching__confidence"
      title={`${formatConfidence(value)} confidence — ${pairToneLabel(tone)}`}
    >
      <span className="matching__confidence-track" aria-hidden="true">
        <span
          className={`matching__confidence-fill matching__confidence-fill--${tone}`}
          style={{ width: `${percent}%` }}
        />
      </span>
      <span className="matching__confidence-value tabular">{formatConfidence(value)}</span>
    </span>
  );
}

function confidenceTone(value: number): StatusTone {
  if (value >= 0.9) return 'ok';
  if (value >= 0.8) return 'info';
  if (value >= 0.7) return 'warn';
  return 'danger';
}

function pairToneLabel(tone: StatusTone): string {
  switch (tone) {
    case 'ok':
      return 'high';
    case 'info':
      return 'fair';
    case 'warn':
      return 'low';
    default:
      return 'very low';
  }
}

/** Two decimals, always — confidences line up as a column. */
function formatConfidence(value: number): string {
  return value.toFixed(2);
}
