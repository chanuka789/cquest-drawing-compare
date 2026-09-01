/**
 * Temporary Phase 1 screen: prove the UI can reach the engine.
 * Task 1.7 replaces this with the Setup screen.
 */

import { useEffect } from 'react';

import { useAppStore } from './store/appStore';
import './App.css';

export default function App() {
  const { status, health, errorMessage, desktop, connect } = useAppStore();

  useEffect(() => {
    void connect();
  }, [connect]);

  return (
    <main className="boot">
      <div className="boot__seam" aria-hidden="true" />

      <h1>{health?.app_name ?? 'C-Quest Drawing Compare'}</h1>

      {status === 'connecting' && <p className="boot__note">Connecting to the engine…</p>}

      {status === 'error' && (
        <>
          <p className="boot__note boot__note--error">{errorMessage}</p>
          <button type="button" className="boot__retry" onClick={() => void connect()}>
            Try again
          </button>
        </>
      )}

      {status === 'ready' && health && (
        <dl className="boot__facts tabular">
          <dt>Engine</dt>
          <dd>{health.status}</dd>

          <dt>Version</dt>
          <dd>{health.version}</dd>

          <dt>Mode</dt>
          <dd>{health.dev_mode ? 'Development' : 'Production'}</dd>

          <dt>Shell</dt>
          <dd>{desktop ? 'Desktop window' : 'Browser'}</dd>

          <dt>AI</dt>
          <dd>{health.ai_mode}</dd>

          <dt>Schema</dt>
          <dd>{health.schema_version}</dd>

          <dt>App data</dt>
          <dd className="boot__path">{health.paths.root}</dd>
        </dl>
      )}
    </main>
  );
}
