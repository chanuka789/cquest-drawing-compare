import { useState } from 'react';

import { baseName, truncateMiddle } from '../../lib/path';

interface IssuePanelProps {
  label: string;
  hint: string;
  folder: string | null;
  onChoose: () => void;
  onDropUnsupported: () => void;
}

/**
 * One of the two folder targets.
 *
 * The whole card is a single button, so it is reachable by Tab, activates on
 * Enter and Space, and shows one focus ring rather than three.
 */
export function IssuePanel({
  label,
  hint,
  folder,
  onChoose,
  onDropUnsupported,
}: IssuePanelProps) {
  const [dragOver, setDragOver] = useState(false);
  const chosen = folder !== null;

  const className = [
    'panel',
    chosen ? 'panel--chosen' : 'panel--empty',
    dragOver ? 'panel--dragover' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <button
      type="button"
      className={className}
      onClick={onChoose}
      onDragOver={(event) => {
        event.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragOver(false);
        // Windows does not hand a real folder path to the web layer on drop.
        // Say what to do instead of failing silently.
        onDropUnsupported();
      }}
      aria-label={chosen ? `${label}. ${folder}. Choose a different folder` : `${label}. ${hint}`}
    >
      <span className="panel__label">{label}</span>

      {chosen ? (
        <>
          <span className="panel__name">{baseName(folder)}</span>
          <span className="panel__path tabular" title={folder}>
            {truncateMiddle(folder, 46)}
          </span>
          <span className="panel__rule" aria-hidden="true" />
          <span className="panel__meta tabular">Not scanned yet</span>
          <span className="panel__change">Change</span>
        </>
      ) : (
        <>
          <span className="panel__empty">Drop a folder here</span>
          <span className="panel__empty-sub">or browse</span>
        </>
      )}
    </button>
  );
}
