import { StatusPill, type StatusTone } from '../StatusPill';
import { useAppStore, type ScreenName } from '../../store/appStore';

import './TopBar.css';

const AI_MODE_LABEL: Record<string, string> = {
  offline: 'Offline mode',
  local: 'Local model',
  cloud: 'Cloud AI',
};

/**
 * The stages a user can jump to.
 *
 * Every one of these is reachable at any time. A stage with nothing in it
 * yet says so on arrival — which is a far better answer than a disabled
 * button that never explains what would enable it.
 */
const STAGES: { screen: ScreenName; label: string }[] = [
  { screen: 'setup', label: 'Set up' },
  { screen: 'register', label: 'Register' },
  { screen: 'matching', label: 'Matching' },
  { screen: 'alignment', label: 'Alignment' },
  { screen: 'changes', label: 'Changes' },
  { screen: 'rename', label: 'Rename' },
];

/** The application chrome. The only place brand red is allowed to appear. */
export function TopBar() {
  const status = useAppStore((store) => store.status);
  const health = useAppStore((store) => store.health);
  const errorMessage = useAppStore((store) => store.errorMessage);
  const screen = useAppStore((store) => store.screen);
  const go = useAppStore((store) => store.go);
  const goToHome = useAppStore((store) => store.goToHome);

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
      <button
        type="button"
        className="topbar__brand"
        onClick={goToHome}
        title="Back to the start"
      >
        <span className="topbar__mark" aria-hidden="true" />
        <span className="topbar__title">C-Quest Drawing Compare</span>
      </button>

      {screen !== 'home' && (
        <nav className="topbar__nav" aria-label="Stages">
          {STAGES.map((stage) => (
            <button
              key={stage.screen}
              type="button"
              className="topbar__tab"
              aria-current={screen === stage.screen ? 'page' : undefined}
              onClick={() => go(stage.screen)}
            >
              {stage.label}
            </button>
          ))}
        </nav>
      )}

      <div className="topbar__right">
        <StatusPill tone={tone} title={hint}>
          {label}
        </StatusPill>
      </div>
    </header>
  );
}
