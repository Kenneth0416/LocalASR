import { Routes, Route } from 'react-router-dom';
import AppShell from './components/AppShell';
import Dashboard from './pages/Dashboard';
import MeetingRoom from './pages/MeetingRoom';
import Upload from './pages/Upload';
import Library from './pages/Library';

function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/live" element={<MeetingRoom />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/library" element={<Library />} />
      </Routes>
    </AppShell>
  );
}

export default App;
