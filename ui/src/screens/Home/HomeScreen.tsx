/**
 * The home screen: pick a job.
 *
 * Comparing drawings and renaming them are two separate jobs. Someone with
 * a messy folder of consultant files wants to rename them and nothing else;
 * someone chasing a revision wants the changes and does not care what the
 * files are called. Forcing either through the other's screens was the main
 * thing wrong with the old flow, so both start here, side by side.
 */

import { useEffect } from 'react';

import { useAppStore } from '../../store/appStore';
import { useSetupStore } from '../../store/setupStore';

import './HomeScreen.css';

export function HomeScreen() {
  const startTool = useAppStore((store) => store.startTool);
  const go = useAppStore((store) => store.go);
  const oldSide = useSetupStore((store) => store.old);
  const newSide = useSetupStore((store) => store.new);
  const init = useSetupStore((store) => store.init);

  useEffect(() => {
    void init();
  }, [init]);

  const foldersChosen = Boolean(oldSide.folder && newSide.folder);
  const sheetsFound = oldSide.sheet_count > 0 && newSide.sheet_count > 0;

  return (
    <main className="home">
      <div className="home__inner">
        <header className="home__head">
          <h1 className="home__title">What would you like to do?</h1>
          <p className="home__sub">
            Both tools work on their own. You can compare a set without renaming
            anything, and rename a set without comparing it.
          </p>
        </header>

        <div className="home__tools">
          <ToolCard
            name="Compare drawings"
            description="Find what changed between the previous issue and the current one, sheet by sheet."
            steps={['Point to both issues', 'The app matches and aligns them', 'Walk the change list']}
            actionLabel={foldersChosen ? 'Continue comparing' : 'Start comparing'}
            onClick={() => startTool('compare')}
          />
          <ToolCard
            name="Rename drawings"
            description="Give a folder of files consistent names, built from what each sheet says about itself."
            steps={['Point to the files', 'Build a naming template', 'Review the plan, then apply']}
            actionLabel={foldersChosen ? 'Continue renaming' : 'Start renaming'}
            onClick={() => startTool('rename')}
          />
        </div>

        {sheetsFound && (
          <section className="home__resume" aria-label="Open a stage directly">
            <h2 className="home__resume-title">Or open a stage directly</h2>
            <div className="home__resume-links">
              <button type="button" className="home__link" onClick={() => go('register')}>
                Drawing register
              </button>
              <button type="button" className="home__link" onClick={() => go('matching')}>
                Matching
              </button>
              <button type="button" className="home__link" onClick={() => go('alignment')}>
                Alignment
              </button>
              <button type="button" className="home__link" onClick={() => go('changes')}>
                Changes
              </button>
              <button type="button" className="home__link" onClick={() => go('rename')}>
                Rename
              </button>
            </div>
          </section>
        )}
      </div>
    </main>
  );
}

interface ToolCardProps {
  name: string;
  description: string;
  steps: string[];
  actionLabel: string;
  onClick: () => void;
}

function ToolCard({ name, description, steps, actionLabel, onClick }: ToolCardProps) {
  return (
    <article className="home__tool">
      <h2 className="home__tool-name">{name}</h2>
      <p className="home__tool-desc">{description}</p>
      <ol className="home__tool-steps">
        {steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
      <button type="button" className="button button--primary home__tool-go" onClick={onClick}>
        {actionLabel}
      </button>
    </article>
  );
}
