export default function Header({ title, children }) {
  return (
    <header style={{
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '10px 18px', borderBottom: '1px solid var(--border)',
      background: 'var(--surface)', flexShrink: 0, zIndex: 10,
    }}>
      <h1 style={{
        fontSize: 15, fontWeight: 700, letterSpacing: '-0.03em', color: 'var(--text)',
        flex: 1,
      }}>
        {title}
      </h1>
      {children}
    </header>
  );
}
