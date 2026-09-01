import type { ProgressEvent } from '../../api/types';
import { formatEta } from '../../hooks/useProgress';

import './ProgressRail.css';

interface ProgressRailProps {
  event: ProgressEvent | null;
  onCancel?: () => void;
}

/**
 * A real progress rail, not a spinner.
 *
 * Trust comes from visible progress: the current file name, how far through
 * we are, and how long is left. A cancel button that does nothing is worse
 * than no cancel button, so it is only shown while work is actually running.
 */
export function ProgressRail({ event, onCancel }: ProgressRailProps) {
  if (!event || event.kind === 'finished' || event.kind === 'cancelled') return null;

  const failed = event.kind === 'failed';
  const percent = Math.round(event.fraction * 100);
  const eta = formatEta(event.eta);

  return (
    <div className={`rail${failed ? ' rail--failed' : ''}`} role="status" aria-live="polite">
      <div className="rail__track" aria-hidden="true">
        <div className="rail__fill" style={{ width: `${percent}%` }} />
      </div>

      <div className="rail__line">
        <span className="rail__count tabular">
          {event.total > 0 ? `${event.current} of ${event.total}` : 'Starting…'}
        </span>

        <span className="rail__item" title={event.current_item}>
          {failed ? event.message : event.current_item}
        </span>

        {eta && !failed && <span className="rail__eta tabular">{eta}</span>}

        {onCancel && !failed && (
          <button type="button" className="rail__cancel" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
