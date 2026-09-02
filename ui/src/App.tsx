import { useEffect } from 'react';

import { TopBar } from './components/TopBar';
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
      {screen === 'setup' ? <SetupScreen /> : <RegisterScreen />}
    </div>
  );
}
