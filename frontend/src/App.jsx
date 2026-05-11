import React from 'react';
import { Routes, Route } from 'react-router-dom';
import { ToastProvider, useToast } from './components/Toast';
import { setToastCallback } from './api/client';
import AppShell from './components/AppShell';
import Dashboard from './pages/Dashboard';
import MeetingRoom from './pages/MeetingRoom';
import Upload from './pages/Upload';
import Library from './pages/Library';
import Templates from './pages/Templates';

function App() {
  return (
    <ToastProvider>
      <AppInner />
    </ToastProvider>
  );
}

function AppInner() {
  const addToast = useToast();

  React.useEffect(() => {
    setToastCallback(addToast);
  }, [addToast]);

  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/live" element={<MeetingRoom />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/library" element={<Library />} />
        <Route path="/templates" element={<Templates />} />
      </Routes>
    </AppShell>
  );
}

export default App;
