import { useEffect } from 'react';

import { TopBar } from './components/TopBar';
import { AlignmentScreen } from './screens/Alignment';
import { ManualAlignScreen } from './screens/Manual';
import { MatchingScreen } from './screens/Matching';
import { RegisterScreen } from './screens/Register';
import { RenameScreen } from './screens/Rename';
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
      {screen === 'rename' && <RenameScreen />}
      {screen === 'alignment' && <AlignmentScreen />}
      {screen === 'manual' && <ManualAlignScreen />}
    </div>
  );
}
