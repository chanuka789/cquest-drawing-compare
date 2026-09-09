import { useEffect } from 'react';

import { TopBar } from './components/TopBar';
import { MatchingScreen } from './screens/Matching';
import { RegisterScreen } from './screens/Register';
import { SetupScreen } from './screens/Setup';
import { useAppStore } from './store/appStore';

import './App.css';

export default function App() {
  const connect = useAppStore((state) => state.connect);
  const screen = useAppStore((state) => state.screen);

  useEffect(() => {
    void connect();
  }, [connect]);

  return (
    <div className="shell">
      <TopBar />
      {screen === 'setup' && <SetupScreen />}
      {screen === 'register' && <RegisterScreen />}
      {screen === 'matching' && <MatchingScreen />}
      {screen === 'rename' && <RenamePlaceholder />}
    </div>
  );
}

/** The rename step lands here until its own screen is built. */
function RenamePlaceholder() {
  const goToMatching = useAppStore((state) => state.goToMatching);
  return (
    <div className="rename">
      <div className="rename__card">
        <h1 className="rename__title">Rename review screen comes next</h1>
        <button type="button" className="button button--primary" onClick={goToMatching}>
          ← Back to matching
        </button>
      </div>
    </div>
  );
}
