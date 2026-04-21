import { useState } from 'react';
import { Pencil, Check, X } from 'lucide-react';
import { formatDuration } from '../utils/formatters';
import api from '../api/client';

export default function TranscriptSegment({ segment, sessionId, onUpdate }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(segment.text);
  const [saving, setSaving] = useState(false);

  const startTime = segment.start_time ?? segment.start ?? 0;
  const endTime = segment.end_time ?? segment.end ?? 0;

  const handleSave = async () => {
    if (!text.trim() || text === segment.text) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await api.patch(`/meetings/${sessionId}/transcript/${segment.id}`, { text });
      onUpdate?.(segment.id, text);
      setEditing(false);
    } catch {
      // error handled by interceptor
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{
      display: 'flex', gap: 12, padding: '10px 14px',
      borderBottom: '1px solid var(--border)',
    }}>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', flexShrink: 0, paddingTop: 2 }}>
        {formatDuration(startTime)}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--accent)', marginBottom: 2 }}>
          {segment.speaker || 'Speaker'}
        </div>
        {editing ? (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSave()}
              style={{
                flex: 1, padding: '4px 8px', borderRadius: 6,
                border: '1px solid var(--border)', background: 'var(--surface2)',
                color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
              }}
              autoFocus
            />
            <button onClick={handleSave} disabled={saving} style={{ color: 'var(--green)' }}>
              <Check size={14} />
            </button>
            <button onClick={() => { setEditing(false); setText(segment.text); }} style={{ color: 'var(--text-muted)' }}>
              <X size={14} />
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <span style={{ fontSize: 13, lineHeight: 1.6, flex: 1 }}>{segment.text}</span>
            <button
              onClick={() => setEditing(true)}
              style={{ color: 'var(--text-muted)', opacity: 0, transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = 1; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = 0; }}
            >
              <Pencil size={12} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
