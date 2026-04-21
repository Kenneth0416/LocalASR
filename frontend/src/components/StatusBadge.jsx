const STATUS_CONFIG = {
  live: { label: '進行中', color: 'var(--red)', bg: 'var(--red-dim)' },
  completed: { label: '已完成', color: 'var(--green)', bg: 'var(--green-dim)' },
  processing: { label: '處理中', color: 'var(--yellow)', bg: 'var(--yellow-dim)' },
  failed: { label: '失敗', color: 'var(--red)', bg: 'var(--red-dim)' },
};

export default function StatusBadge({ status }) {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.completed;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '2px 8px', borderRadius: 12,
      fontSize: 11, fontWeight: 600,
      color: config.color,
      background: config.bg,
    }}>
      {status === 'live' && (
        <span style={{
          width: 6, height: 6, borderRadius: '50%',
          background: 'var(--red)', animation: 'pulse 1.4s infinite',
        }} />
      )}
      {config.label}
    </span>
  );
}
