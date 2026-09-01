import { useEffect } from 'react';

import { TopBar } from './components/TopBar';
import { SetupScreen } from './screens/Setup';
import { useAppStore } from './store/appStore';

import './App.css';

export default function App() {
  const connect = useAppStore((state) => state.connect);

  useEffect(() => {
    void connect();
  }, [connect]);

  return (
    <div className="shell">
      <TopBar />
      <SetupScreen />
    </div>
  );
}
