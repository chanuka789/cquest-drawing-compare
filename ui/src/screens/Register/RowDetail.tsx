import { useState } from 'react';

import { StatusPill } from '../../components/StatusPill';
import type { RegisterRow } from '../../api/types';
import { SOURCE_LABEL, STATUS_LABEL, STATUS_TONE } from './statusMeta';

interface RowDetailProps {
  row: RegisterRow;
  unidentifiedCount: number;
  onClose: () => void;
  onCorrect: (drawingNo: string, applyToAll: boolean) => void;
}

/** Everything known about one drawing, with a way to correct its number. */
export function RowDetail({ row, unidentifiedCount, onClose, onCorrect }: RowDetailProps) {
  const [value, setValue] = useState(row.drawing_no);
  const [applyToAll, setApplyToAll] = useState(false);

  const canCorrect = Boolean(row.new_path ?? row.old_path);
  const others = Math.max(0, unidentifiedCount - 1);

  return (
    <aside className="detail" aria-label={`Details for ${row.drawing_no}`}>
      <header className="detail__head">
        <div>
          <h2 className="detail__title">{row.drawing_no}</h2>
          {row.title && <p className="detail__subtitle">{row.title}</p>}
        </div>
        <button type="button" className="detail__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <StatusPill tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</StatusPill>

      <p className="detail__note">{row.note}</p>

      <dl className="detail__facts tabular">
        <dt>Previous revision</dt>
        <dd>{row.old_revision ?? '—'}</dd>
        <dt>Current revision</dt>
        <dd>{row.new_revision ?? '—'}</dd>
        <dt>Number found by</dt>
        <dd>{SOURCE_LABEL[row.source_of_number] ?? row.source_of_number}</dd>
        <dt>On the drawing list</dt>
        <dd>{row.in_drawing_list === null ? 'No list imported' : row.in_drawing_list ? 'Yes' : 'No'}</dd>
      </dl>

      {row.number_mismatch && (
        <p className="detail__warning">
          The title block and the file name give different drawing numbers. Check which is
          correct.
        </p>
      )}

      <div className="detail__paths">
        <span className="detail__path-label micro">Previous issue</span>
        <span className="detail__path" title={row.old_path ?? undefined}>
          {row.old_path ?? 'Not in the previous issue'}
        </span>
        <span className="detail__path-label micro">Current issue</span>
        <span className="detail__path" title={row.new_path ?? undefined}>
          {row.new_path ?? 'Not in the current issue'}
        </span>
      </div>

      {row.superseded_paths.length > 0 && (
        <p className="detail__warning micro">
          {row.superseded_paths.length} older revision
          {row.superseded_paths.length === 1 ? '' : 's'} of this drawing were found in the same
          folder and ignored.
        </p>
      )}

      {canCorrect && (
        <form
          className="detail__correct"
          onSubmit={(event) => {
            event.preventDefault();
            if (value.trim()) onCorrect(value.trim(), applyToAll);
          }}
        >
          <label className="field">
            <span className="field__label">Drawing number</span>
            <input
              className="field__control tabular"
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
          </label>

          {others > 0 && (
            <label className="detail__apply">
              <input
                type="checkbox"
                checked={applyToAll}
                onChange={(event) => setApplyToAll(event.target.checked)}
              />
              <span>
                Apply this pattern to the other {others} unidentified{' '}
                {others === 1 ? 'sheet' : 'sheets'}
              </span>
            </label>
          )}

          <button type="submit" className="button button--primary detail__save">
            Save number
          </button>
        </form>
      )}
    </aside>
  );
}
