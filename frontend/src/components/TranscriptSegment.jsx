import { useState, useRef, useCallback, useEffect } from 'react';
import { Pencil, Check, X } from 'lucide-react';
import { formatTimeRange } from '../utils/formatters';
import api from '../api/client';

export default function TranscriptSegment({ segment, sessionId, onUpdate }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(segment.text);
  const [saving, setSaving] = useState(false);
  const longPressTimer = useRef(null);
  const longPressTriggered = useRef(false);

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

  const handleTouchStart = useCallback(() => {
    longPressTriggered.current = false;
    longPressTimer.current = setTimeout(() => {
      longPressTriggered.current = true;
      setEditing(true);
    }, 500);
  }, []);

  const handleTouchEnd = useCallback(() => {
    clearTimeout(longPressTimer.current);
  }, []);

  useEffect(() => {
    return () => clearTimeout(longPressTimer.current);
  }, []);

  return (
    <div
      className="transcript-segment-enter"
      style={{
        display: 'flex', gap: 12, padding: '10px 14px',
        borderBottom: '1px solid var(--border)',
      }}
    >
      <div style={{
        fontSize: 11, color: 'var(--text-muted)',
        fontFamily: 'var(--font-mono)', flexShrink: 0, paddingTop: 2,
      }}>
        {formatTimeRange(startTime, endTime)}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
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
            <button onClick={handleSave} disabled={saving} style={{ color: 'var(--green)' }} aria-label="儲存">
              <Check size={14} />
            </button>
            <button onClick={() => { setEditing(false); setText(segment.text); }} style={{ color: 'var(--text-muted)' }} aria-label="取消">
              <X size={14} />
            </button>
          </div>
        ) : (
          <div
            className="transcript-row"
            style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}
            onTouchStart={handleTouchStart}
            onTouchEnd={handleTouchEnd}
            onTouchCancel={handleTouchEnd}
          >
            <span style={{ fontSize: 13, lineHeight: 1.6, flex: 1 }}>{segment.text}</span>
            <button
              className="edit-btn"
              onClick={() => setEditing(true)}
              style={{ color: 'var(--text-muted)', flexShrink: 0, padding: 2 }}
              aria-label="編輯轉錄"
            >
              <Pencil size={12} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
