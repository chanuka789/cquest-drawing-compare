import { useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import type { SheetRow } from '../../api/types';

const ROW_HEIGHT = 34;

/** Short marker for where the number came from, explained on hover. */
const SOURCE_MARK: Record<string, string> = {
  titleblock: 'TB',
  sheet_text: 'TX',
  filename: 'FN',
  drawing_list: 'DL',
  user: 'ME',
  ai: 'AI',
  none: '—',
};

interface SheetListProps {
  rows: SheetRow[];
  filter: string;
  onFilterChange: (text: string) => void;
  isScanning: boolean;
}

/**
 * The drawing list a folder panel expands into.
 *
 * Virtualised, because a real issue folder holds hundreds of sheets and the
 * register can hold thousands. Rows appear from the fast pass with nothing
 * but a file name and fill in as the deep pass reports each sheet.
 */
export function SheetList({ rows, filter, onFilterChange, isScanning }: SheetListProps) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  return (
    <div className="sheets">
      <div className="sheets__tools">
        <input
          type="search"
          className="sheets__filter"
          placeholder="Filter by number, title or file name"
          value={filter}
          onChange={(event) => onFilterChange(event.target.value)}
          aria-label="Filter this drawing list"
        />
        <span className="sheets__count tabular micro">{rows.length} shown</span>
      </div>

      <div className="sheets__head micro">
        <span>Drawing no.</span>
        <span>Title</span>
        <span className="sheets__cell--centre">Rev</span>
        <span className="sheets__cell--centre">Pages</span>
        <span className="sheets__cell--centre">Source</span>
      </div>

      <div className="sheets__scroll" ref={parentRef}>
        {rows.length === 0 && (
          <p className="sheets__empty">
            {isScanning ? 'Reading the drawings…' : 'No drawings match this filter.'}
          </p>
        )}

        <div className="sheets__canvas" style={{ height: `${virtualizer.getTotalSize()}px` }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const row = rows[virtualRow.index];
            if (!row) return null;

            const unread = !row.is_readable;
            const unidentified = !unread && !row.drawing_no;
            const flagged = unread || unidentified || row.number_mismatch;

            return (
              <div
                key={`${row.abs_path}:${row.page_index}`}
                className={`sheets__row${flagged ? ' sheets__row--flagged' : ''}`}
                style={{
                  transform: `translateY(${virtualRow.start}px)`,
                  height: `${virtualRow.size}px`,
                }}
                title={row.warnings.join(' ') || undefined}
              >
                <span className="sheets__no tabular">
                  {row.drawing_no ?? <span className="sheets__pending">{row.filename}</span>}
                </span>

                <span className="sheets__title" title={row.title ?? undefined}>
                  {row.title ?? (row.is_readable ? '' : 'Could not be read')}
                </span>

                <span className="sheets__cell--centre tabular">{row.revision ?? ''}</span>

                <span className="sheets__cell--centre tabular">
                  {row.page_count > 1 ? `${row.page_index + 1}/${row.page_count}` : '1'}
                </span>

                <span
                  className="sheets__cell--centre sheets__source micro"
                  title={row.source_explanation || undefined}
                >
                  {flagged ? '⚠' : (SOURCE_MARK[row.source_of_number] ?? '—')}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
