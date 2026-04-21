import { useState, useEffect } from 'react';
import { NavLink } from 'react-router-dom';
import { LayoutDashboard, Mic, Upload, Library, Sun, Moon } from 'lucide-react';

const navItems = [
  { id: 'dashboard', label: '首頁', path: '/', icon: LayoutDashboard },
  { id: 'meeting', label: '直播會議室', path: '/live', icon: Mic },
  { id: 'upload', label: '上傳轉錄', path: '/upload', icon: Upload },
  { id: 'library', label: '會議紀錄庫', path: '/library', icon: Library },
];

export default function Sidebar() {
  const [dark, setDark] = useState(() => {
    try { return JSON.parse(localStorage.getItem('ms-dark') ?? 'true'); }
    catch { return true; }
  });

  useEffect(() => {
    document.body.classList.toggle('light', !dark);
    localStorage.setItem('ms-dark', JSON.stringify(dark));
  }, [dark]);

  return (
    <aside style={{
      width: 'var(--sidebar-width)',
      background: 'var(--surface)',
      borderRight: '1px solid var(--border)',
      display: 'flex',
      flexDirection: 'column',
      flexShrink: 0,
    }}>
      {/* Logo */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '12px 14px', borderBottom: '1px solid var(--border)',
      }}>
        <div style={{
          width: 24, height: 24, borderRadius: 6,
          background: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
            <circle cx="10" cy="10" r="9" stroke="white" strokeWidth="1.5"/>
            <path d="M6 7h8M6 10h6M6 13h4" stroke="white" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </div>
        <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--accent)' }}>MeetScribe</span>
      </div>

      {/* Navigation */}
      <nav style={{ flex: 1, padding: '8px 6px', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {navItems.map((item) => (
          <NavLink
            key={item.id}
            to={item.path}
            style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '7px 10px', borderRadius: 7,
              fontSize: 12, fontWeight: 500, textDecoration: 'none',
              color: isActive ? 'var(--accent)' : 'var(--text-muted)',
              background: isActive ? 'var(--accent-dim)' : 'transparent',
              transition: 'all 0.15s',
            })}
          >
            <item.icon size={15} />
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* Bottom: theme toggle */}
      <div style={{ padding: '8px 6px', borderTop: '1px solid var(--border)' }}>
        <button
          onClick={() => setDark((d) => !d)}
          style={{
            display: 'flex', alignItems: 'center', gap: 8,
            width: '100%', padding: '7px 10px', borderRadius: 7,
            fontSize: 12, fontWeight: 500, color: 'var(--text-muted)',
            background: 'none', border: 'none', cursor: 'pointer',
          }}
        >
          {dark ? <Sun size={15} /> : <Moon size={15} />}
          {dark ? '切換淺色' : '切換深色'}
        </button>
      </div>
    </aside>
  );
}
