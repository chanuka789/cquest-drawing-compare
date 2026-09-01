import { StatusPill, type StatusTone } from '../StatusPill';
import { useAppStore } from '../../store/appStore';

import './TopBar.css';

const AI_MODE_LABEL: Record<string, string> = {
  offline: 'Offline mode',
  local: 'Local model',
  cloud: 'Cloud AI',
};

/** The application chrome. The only place brand red is allowed to appear. */
export function TopBar() {
  const { status, health, errorMessage } = useAppStore();

  let tone: StatusTone = 'neutral';
  let label = 'Connecting to the engine…';
  let hint: string | undefined;

  if (status === 'ready' && health) {
    tone = 'ok';
    label = AI_MODE_LABEL[health.ai_mode] ?? health.ai_mode;
    hint = `Engine ${health.version} · ${health.dev_mode ? 'development' : 'production'} · ${health.paths.root}`;
  } else if (status === 'error') {
    tone = 'warn';
    label = 'Engine not connected';
    hint = errorMessage ?? undefined;
  }

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__mark" aria-hidden="true" />
        <span className="topbar__title">C-Quest Drawing Compare</span>
      </div>

      <div className="topbar__right">
        <StatusPill tone={tone} title={hint}>
          {label}
        </StatusPill>
      </div>
    </header>
  );
}
