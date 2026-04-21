import StatusBadge from './StatusBadge';
import { formatRelativeDate } from '../utils/formatters';
import { Clock, CheckSquare } from 'lucide-react';

export default function MeetingCard({ meeting, onClick }) {
  const status = meeting.status || 'completed';

  return (
    <div
      onClick={onClick}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 12,
        padding: '14px 16px', borderBottom: '1px solid var(--border)',
        cursor: 'pointer', transition: 'background 0.15s',
      }}
      onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface2)'; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
    >
      <div style={{
        width: 36, height: 36, borderRadius: 9,
        background: 'var(--accent-dim)', color: 'var(--accent)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 14, fontWeight: 700, flexShrink: 0,
      }}>
        {(meeting.title || '?')[0]}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>{meeting.title}</span>
          <StatusBadge status={status} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, color: 'var(--text-muted)' }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <Clock size={12} />
            {formatRelativeDate(meeting.ended_at || meeting.created_at)}
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <CheckSquare size={12} />
            {meeting.transcript?.length || 0} 條記錄
          </span>
        </div>
      </div>
    </div>
  );
}
