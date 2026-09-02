import type { IssueSide, SheetRow, SideState } from '../../api/types';
import { baseName, truncateMiddle } from '../../lib/path';
import { SheetList } from './SheetList';

interface IssuePanelProps {
  label: string;
  side: IssueSide;
  state: SideState;
  rows: SheetRow[];
  expanded: boolean;
  filter: string;
  onChoose: () => void;
  onToggle: () => void;
  onFilterChange: (text: string) => void;
  onDropUnsupported: () => void;
}

/**
 * One of the two folder targets.
 *
 * Empty, the whole card is a single button, so it is reachable by Tab and
 * activates on Enter and Space. Once a folder is chosen the card expands into
 * a drawing list, and the controls inside it become their own focus targets.
 */
export function IssuePanel({
  label,
  side,
  state,
  rows,
  expanded,
  filter,
  onChoose,
  onToggle,
  onFilterChange,
  onDropUnsupported,
}: IssuePanelProps) {
  const chosen = state.folder !== null;

  if (!chosen) {
    return (
      <button
        type="button"
        className="panel panel--empty"
        onClick={onChoose}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          // Windows does not hand a real folder path to the web layer on drop.
          onDropUnsupported();
        }}
        aria-label={`${label}. Drop a folder here or browse`}
      >
        <span className="panel__label">{label}</span>
        <span className="panel__empty">Drop a folder here</span>
        <span className="panel__empty-sub">or browse</span>
      </button>
    );
  }

  return (
    <section className="panel panel--chosen" aria-label={label}>
      <header className="panel__head">
        <div className="panel__identity">
          <span className="panel__label">{label}</span>
          <span className="panel__name">{baseName(state.folder ?? '')}</span>
          <span className="panel__path tabular" title={state.folder ?? undefined}>
            {truncateMiddle(state.folder ?? '', 46)}
          </span>
        </div>

        <div className="panel__actions">
          <button type="button" className="panel__button" onClick={onChoose}>
            Change
          </button>
          <button
            type="button"
            className="panel__button"
            onClick={onToggle}
            aria-expanded={expanded}
          >
            {expanded ? 'Collapse' : 'Show drawings'}
          </button>
        </div>
      </header>

      <p className="panel__meta tabular">
        {state.headline}
        {state.is_scanning && <span className="panel__reading"> · reading…</span>}
      </p>

      {state.error && <p className="panel__error">{state.error}</p>}

      {Object.keys(state.other_files).length > 0 && (
        <p className="panel__other micro">
          Also found:{' '}
          {Object.entries(state.other_files)
            .map(([extension, count]) => `${count} ${extension.replace('.', '').toUpperCase()}`)
            .join(', ')}
          . Only PDFs are compared.
        </p>
      )}

      {expanded && (
        <SheetList
          rows={rows}
          filter={filter}
          onFilterChange={onFilterChange}
          isScanning={state.is_scanning}
        />
      )}

      <span className="visually-hidden">{`${label}: ${state.headline}`}</span>
      <span hidden>{side}</span>
    </section>
  );
}
