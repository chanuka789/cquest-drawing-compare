import { useEffect, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import { StatusPill } from '../../components/StatusPill';
import type { RegisterStatus } from '../../api/types';
import { useAppStore } from '../../store/appStore';
import { useRegisterStore, visibleRows } from '../../store/registerStore';
import { IssueTypeDialog } from './IssueTypeDialog';
import { RowDetail } from './RowDetail';
import { STATUS_LABEL, STATUS_TONE, SUMMARY_ORDER } from './statusMeta';

import './RegisterScreen.css';

const ROW_HEIGHT = 40;

/**
 * Screen 2 — the drawing register.
 *
 * One row per drawing, with the previous and current issues either side of
 * the seam. Rows needing attention sort to the top, because they are the
 * reason to open the register at all.
 */
export function RegisterScreen() {
  const state = useRegisterStore();
  const goToSetup = useAppStore((store) => store.goToSetup);
  const rows = visibleRows(state);
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void state.build();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 16,
  });

  const unidentified = state.rows.filter((row) => row.status === 'unidentified').length;

  if (state.question) {
    return (
      <IssueTypeDialog
        question={state.question}
        onAnswer={(answer) => void state.answerIssueType(answer)}
        onBack={goToSetup}
      />
    );
  }

  return (
    <div className="register">
      <header className="register__head">
        <div className="register__title-row">
          <button type="button" className="register__back" onClick={goToSetup}>
            ← Setup
          </button>
          <h1>Drawing register</h1>
        </div>

        {state.summary.sentence && <p className="register__sentence">{state.summary.sentence}</p>}
      </header>

      {/* ── Summary bar: every count is a filter ── */}
      <div className="register__summary">
        {SUMMARY_ORDER.filter((status) => (state.summary.counts[status] ?? 0) > 0).map(
          (status) => (
            <button
              key={status}
              type="button"
              className={`chip${state.statusFilter === status ? ' chip--on' : ''}`}
              onClick={() => state.setStatusFilter(status)}
              aria-pressed={state.statusFilter === status}
            >
              <StatusPill tone={STATUS_TONE[status]}>
                <span className="tabular">{state.summary.counts[status]}</span>{' '}
                {STATUS_LABEL[status].toLowerCase()}
              </StatusPill>
            </button>
          ),
        )}

        {state.summary.attention_count > 0 && (
          <button
            type="button"
            className={`chip${state.attentionOnly ? ' chip--on' : ''}`}
            onClick={() => state.toggleAttentionOnly()}
            aria-pressed={state.attentionOnly}
          >
            <StatusPill tone="warn">
              <span className="tabular">{state.summary.attention_count}</span> need attention
            </StatusPill>
          </button>
        )}
      </div>

      {/* ── Tools ── */}
      <div className="register__tools">
        <input
          type="search"
          className="register__search"
          placeholder="Search by drawing number, title or note"
          value={state.search}
          onChange={(event) => state.setSearch(event.target.value)}
          aria-label="Search the register"
        />
        <span className="register__count tabular micro">
          {rows.length} of {state.rows.length} shown
        </span>
        <button
          type="button"
          className="button button--primary register__export"
          onClick={() => void state.exportToExcel()}
          disabled={state.rows.length === 0}
        >
          Export register
        </button>
      </div>

      {state.notice && (
        <p className="register__notice" role="status">
          {state.notice}
        </p>
      )}
      {state.error && (
        <p className="register__error" role="alert">
          {state.error}
        </p>
      )}

      <div className="register__body">
        <div className="register__table">
          <div className="register__thead micro">
            <button type="button" onClick={() => state.setSort('drawing_no')}>
              Drawing no.
            </button>
            <span>Title</span>
            <button
              type="button"
              className="register__cell--centre"
              onClick={() => state.setSort('old_revision')}
            >
              Old rev
            </button>
            <span className="register__seam-cell" aria-hidden="true" />
            <button
              type="button"
              className="register__cell--centre"
              onClick={() => state.setSort('new_revision')}
            >
              New rev
            </button>
            <button type="button" onClick={() => state.setSort('status')}>
              Status
            </button>
            <span>Note</span>
          </div>

          <div className="register__scroll" ref={parentRef}>
            {state.loading && <p className="register__empty">Building the register…</p>}
            {!state.loading && rows.length === 0 && (
              <p className="register__empty">
                {state.rows.length === 0
                  ? 'No drawings were found. Go back to Setup and check both folders.'
                  : 'No drawings match these filters.'}
              </p>
            )}

            <div
              className="register__canvas"
              style={{ height: `${virtualizer.getTotalSize()}px` }}
            >
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const row = rows[virtualRow.index];
                if (!row) return null;
                const selected = state.selected?.drawing_no === row.drawing_no;

                return (
                  <button
                    key={row.drawing_no}
                    type="button"
                    className={[
                      'register__row',
                      row.needs_attention ? 'register__row--attention' : '',
                      selected ? 'register__row--selected' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    style={{
                      transform: `translateY(${virtualRow.start}px)`,
                      height: `${virtualRow.size}px`,
                    }}
                    onClick={() => state.select(selected ? null : row)}
                  >
                    <span className="register__no tabular">{row.drawing_no}</span>
                    <span className="register__title" title={row.title ?? undefined}>
                      {row.title ?? ''}
                    </span>
                    <span className="register__cell--centre tabular">
                      {row.old_revision ?? '—'}
                    </span>
                    <span className="register__seam-cell" aria-hidden="true" />
                    <span className="register__cell--centre tabular">
                      {row.new_revision ?? '—'}
                    </span>
                    <span className="register__status">
                      <StatusPill tone={STATUS_TONE[row.status]}>
                        {STATUS_LABEL[row.status]}
                      </StatusPill>
                    </span>
                    <span className="register__note" title={row.note}>
                      {row.note}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {state.selected && (
          <RowDetail
            row={state.selected}
            unidentifiedCount={unidentified}
            onClose={() => state.select(null)}
            onCorrect={(drawingNo, applyToAll) => {
              const row = state.selected;
              if (row) void state.correct(row, drawingNo, applyToAll);
            }}
          />
        )}
      </div>
    </div>
  );
}

export type { RegisterStatus };
