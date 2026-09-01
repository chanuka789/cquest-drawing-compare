import { baseName } from '../../lib/path';
import {
  SHEET_PROFILES,
  TOLERANCES,
  canBuildRegister,
  useSetupStore,
} from '../../store/setupStore';
import { IssuePanel } from './IssuePanel';
import { Seam } from './Seam';

import './SetupScreen.css';

const DROP_NOT_SUPPORTED =
  'Windows cannot pass a folder path by dragging. Click the panel to browse for it.';

/**
 * Screen 1 — the two folder pickers.
 *
 * The first screen a user sees every time, so it carries the visual identity:
 * two panels either side of the seam.
 */
export function SetupScreen() {
  const state = useSetupStore();
  const ready = canBuildRegister(state);

  const tolerance = TOLERANCES.find((option) => option.id === state.toleranceId);

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
            hint="Drop a folder here or browse"
            folder={state.previousFolder}
            onChoose={() => void state.chooseFolder('previous')}
            onDropUnsupported={() => state.setNotice(DROP_NOT_SUPPORTED)}
          />

          <Seam pulseToken={state.pulseToken} />

          <IssuePanel
            label="Current issue"
            hint="Drop a folder here or browse"
            folder={state.currentFolder}
            onChoose={() => void state.chooseFolder('current')}
            onDropUnsupported={() => state.setNotice(DROP_NOT_SUPPORTED)}
          />
        </div>

        {state.notice && (
          <p className="setup__notice" role="status">
            {state.notice}
          </p>
        )}

        <div className="setup__list">
          <button
            type="button"
            className="setup__list-button"
            onClick={() => void state.chooseDrawingList()}
          >
            <span className="setup__list-label">Drawing list (optional)</span>
            <span className="setup__list-value tabular" title={state.drawingList ?? undefined}>
              {state.drawingList ? baseName(state.drawingList) : 'Choose a file'}
            </span>
          </button>

          {state.drawingList && (
            <button
              type="button"
              className="setup__list-clear"
              onClick={() => state.clearDrawingList()}
              aria-label="Remove the drawing list"
              title="Remove the drawing list"
            >
              ×
            </button>
          )}
        </div>

        <div className="setup__options">
          <label className="field">
            <span className="field__label">Sheet profile</span>
            <select
              className="field__control"
              value={state.profileId}
              onChange={(event) => state.setProfile(event.target.value)}
            >
              {SHEET_PROFILES.map((profile) => (
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
              onChange={(event) => state.setTolerance(event.target.value)}
            >
              {TOLERANCES.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <p className="setup__options-note micro">
            Tolerance is measured on the drawing, at {tolerance?.millimetres ?? 25} mm at
            true scale.
          </p>
        </div>

        <div className="setup__actions">
          <button
            type="button"
            className="button button--primary"
            disabled={!ready}
            title={ready ? undefined : 'Choose a folder for both issues first'}
            onClick={() => {
              // Phase 2 builds the register. Phase 1 stops here on purpose.
              // eslint-disable-next-line no-console
              console.info('Build the register', {
                previous: state.previousFolder,
                current: state.currentFolder,
                drawingList: state.drawingList,
                profile: state.profileId,
                toleranceMm: tolerance?.millimetres,
              });
            }}
          >
            Build the register →
          </button>
        </div>
      </div>
    </div>
  );
}
