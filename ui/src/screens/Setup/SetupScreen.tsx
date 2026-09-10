import { useEffect } from 'react';

import { ProgressRail } from '../../components/ProgressRail';
import { useProgress } from '../../hooks/useProgress';
import { baseName, truncateMiddle } from '../../lib/path';
import { useAppStore } from '../../store/appStore';
import {
  TOLERANCES,
  canBuildRegister,
  useSetupStore,
  visibleSheets,
} from '../../store/setupStore';
import { IssuePanel } from './IssuePanel';
import { Seam } from './Seam';

import './SetupScreen.css';

const DROP_NOT_SUPPORTED =
  'Windows cannot pass a folder path by dragging. Click the panel to browse for it.';

/**
 * Screen 1 — the three folder pickers.
 *
 * The first screen a user sees every time, so it carries the visual identity:
 * two issue panels either side of the seam, with the output folder below.
 */
export function SetupScreen() {
  const state = useSetupStore();
  const goToRegister = useAppStore((store) => store.goToRegister);
  const goToRename = useAppStore((store) => store.goToRename);
  const goToChanges = useAppStore((store) => store.goToChanges);
  const tool = useAppStore((store) => store.tool);
  const ready = canBuildRegister(state);

  // The scan streams progress; refresh the panels whenever a stage ends.
  const { event } = useProgress((incoming) => {
    if (incoming.kind === 'finished' || incoming.kind === 'cancelled') {
      void state.refreshAll();
    }
  });

  useEffect(() => {
    void state.init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Safety net. The socket is the fast path, but a dropped frame must never
  // leave the panels stuck reporting "reading…" for a scan that has finished.
  const scanning = state.old.is_scanning || state.new.is_scanning;
  useEffect(() => {
    if (!scanning) return;
    const timer = window.setInterval(() => void state.refreshAll(), 1500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanning]);

  const tolerance = TOLERANCES.find((option) => option.id === state.toleranceId);
  const validation = state.outputValidation;

  return (
    <div className="setup">
      <div className="setup__inner">
        <header className="setup__head">
          <h1>Compare two drawing issues</h1>
          <p className="setup__lede">
            Point to the folder for each issue. Subfolders are included.
          </p>
        </header>

        <div className="setup__panels">
          <IssuePanel
            label="Previous issue"
            side="old"
            state={state.old}
            rows={visibleSheets(state, 'old')}
            expanded={state.expanded.old}
            filter={state.filter.old}
            onChoose={() => void state.chooseFolder('old')}
            onToggle={() => state.toggleExpanded('old')}
            onFilterChange={(text) => state.setFilter('old', text)}
            sortKey={state.sort.old}
            ascending={state.sortAscending.old}
            onSort={(key) => state.setSort('old', key)}
            onDropUnsupported={() => state.setNotice(DROP_NOT_SUPPORTED)}
          />

          <Seam pulseToken={state.pulseToken} />

          <IssuePanel
            label="Current issue"
            side="new"
            state={state.new}
            rows={visibleSheets(state, 'new')}
            expanded={state.expanded.new}
            filter={state.filter.new}
            onChoose={() => void state.chooseFolder('new')}
            onToggle={() => state.toggleExpanded('new')}
            onFilterChange={(text) => state.setFilter('new', text)}
            sortKey={state.sort.new}
            ascending={state.sortAscending.new}
            onSort={(key) => state.setSort('new', key)}
            onDropUnsupported={() => state.setNotice(DROP_NOT_SUPPORTED)}
          />
        </div>

        <ProgressRail event={event} onCancel={() => void state.cancel()} />

        {state.notice && (
          <p className="setup__notice" role="status">
            {state.notice}
          </p>
        )}

        {/* ── Output folder ── */}
        <div className="setup__row">
          <button
            type="button"
            className="setup__row-button"
            onClick={() => void state.chooseOutput()}
          >
            <span className="setup__row-label">Output folder</span>
            <span
              className="setup__row-value tabular"
              title={state.outputFolder ?? state.outputSuggestion ?? undefined}
            >
              {state.outputFolder
                ? truncateMiddle(state.outputFolder, 54)
                : (state.outputSuggestion
                    ? `Suggested: ${baseName(state.outputSuggestion)}`
                    : 'Choose a folder')}
            </span>
          </button>
        </div>

        {validation && !validation.is_valid && (
          <p className="setup__error" role="alert">
            {validation.errors[0]}
          </p>
        )}
        {validation?.is_valid &&
          validation.warnings.map((warning) => (
            <p key={warning} className="setup__warning micro">
              {warning}
            </p>
          ))}

        {/* ── Drawing list ── */}
        <div className="setup__row">
          <button
            type="button"
            className="setup__row-button"
            onClick={() => void state.chooseDrawingList()}
          >
            <span className="setup__row-label">Drawing list (optional)</span>
            <span className="setup__row-value tabular" title={state.drawingListPath ?? undefined}>
              {state.drawingListPath ? baseName(state.drawingListPath) : 'Choose a file'}
            </span>
          </button>

          {state.drawingListPath && (
            <button
              type="button"
              className="setup__row-clear"
              onClick={() => void state.removeDrawingList()}
              aria-label="Remove the drawing list"
              title="Remove the drawing list"
            >
              ×
            </button>
          )}
        </div>

        {state.listParse && (
          <div className="setup__list-note">
            <p className="micro">
              {state.listParse.row_count} drawings read from{' '}
              {state.listParse.sheet_name ?? 'the file'}
              {state.listParse.header_row ? `, header on row ${state.listParse.header_row}` : ''}.
            </p>
            {state.listParse.warnings.map((warning) => (
              <p key={warning} className="setup__warning micro">
                {warning}
              </p>
            ))}
          </div>
        )}

        {/* ── Options ── */}
        <div className="setup__options">
          <label className="field">
            <span className="field__label">Sheet profile</span>
            <select
              className="field__control"
              value={state.profileId}
              onChange={(event) => void state.setProfile(event.target.value)}
            >
              {state.profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.label}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="field__label">Tolerance</span>
            <select
              className="field__control tabular"
              value={state.toleranceId}
              onChange={(event) => void state.setTolerance(event.target.value)}
            >
              {TOLERANCES.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <p className="setup__options-note micro">
            Tolerance is measured on the drawing, at {tolerance?.millimetres ?? 25} mm at true
            scale.
          </p>
        </div>

        {/* Where "next" leads depends on the job the user picked on the home
            screen. Comparing and renaming are separate tools, so neither is
            routed through the other's screens; the register stays available
            for anyone who wants to look at it first. */}
        <div className="setup__actions">
          {tool === 'rename' ? (
            <button
              type="button"
              className="button button--primary"
              disabled={!ready}
              title={ready ? undefined : 'Choose a folder for both issues and let the scan finish'}
              onClick={() => goToRename()}
            >
              Build the names →
            </button>
          ) : (
            <button
              type="button"
              className="button button--primary"
              disabled={!ready}
              title={ready ? undefined : 'Choose a folder for both issues and let the scan finish'}
              onClick={() => goToChanges()}
            >
              Find the changes →
            </button>
          )}
          <button
            type="button"
            className="button"
            disabled={!ready}
            title={ready ? undefined : 'Choose a folder for both issues and let the scan finish'}
            onClick={() => goToRegister()}
          >
            Build the register
          </button>
        </div>
      </div>
    </div>
  );
}
