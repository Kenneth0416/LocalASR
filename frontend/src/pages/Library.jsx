import { useState } from 'react';
import { useGet } from '../hooks/useApi';
import api from '../api/client';
import Header from '../components/Header';
import MeetingCard from '../components/MeetingCard';
import EmptyState from '../components/EmptyState';
import StatusBadge from '../components/StatusBadge';
import { Search, Trash2, FileText, FileJson, FileCode } from 'lucide-react';

export default function Library() {
  const { data, loading, refetch } = useGet('/meetings');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [deleting, setDeleting] = useState(false);

  const meetings = data?.meetings || [];

  const filtered = search
    ? meetings.filter((m) =>
        (m.title || '').toLowerCase().includes(search.toLowerCase())
      )
    : meetings;

  const handleDelete = async (id) => {
    if (!confirm('確定要刪除這個會議嗎？此操作無法復原。')) return;
    setDeleting(true);
    try {
      await api.delete(`/meetings/${id}`);
      if (selected?.id === id) setSelected(null);
      refetch();
    } catch {
      // error handled by interceptor
    } finally {
      setDeleting(false);
    }
  };

  const handleExport = (id, format) => {
    window.open(`/api/meetings/${id}/export.${format}`, '_blank');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="會議紀錄庫">
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px', borderRadius: 8,
          border: '1px solid var(--border)', background: 'var(--surface2)',
        }}>
          <Search size={14} style={{ color: 'var(--text-muted)' }} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索會議..."
            style={{
              background: 'none', border: 'none', outline: 'none',
              color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
              width: 180,
            }}
          />
        </div>
      </Header>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* Meeting list */}
        <div style={{
          width: 360, flexShrink: 0,
          borderRight: '1px solid var(--border)',
          overflowY: 'auto', background: 'var(--bg)',
        }}>
          {loading ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)' }}>
              載入中...
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title="尚無會議"
              subtitle={search ? '沒有符合搜索條件的會議' : '開始一場新會議來建立記錄'}
            />
          ) : (
            filtered.map((m) => (
              <MeetingCard
                key={m.id}
                meeting={m}
                onClick={() => setSelected(m)}
              />
            ))
          )}
        </div>

        {/* Detail panel */}
        <div style={{ flex: 1, overflowY: 'auto', background: 'var(--bg)', padding: 24 }}>
          {!selected ? (
            <EmptyState
              title="選擇一個會議"
              subtitle="在左側列表中點擊會議查看詳情"
            />
          ) : (
            <div>
              <div style={{
                display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
                marginBottom: 24,
              }}>
                <div>
                  <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 8 }}>
                    {selected.title || '未命名會議'}
                  </h2>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, color: 'var(--text-muted)' }}>
                    <StatusBadge status={selected.status || 'completed'} />
                    <span>{new Date(selected.created_at).toLocaleString('zh-TW')}</span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button
                    onClick={() => handleExport(selected.id, 'txt')}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      padding: '6px 10px', borderRadius: 6,
                      background: 'var(--surface2)', border: '1px solid var(--border)',
                      color: 'var(--text-muted)', fontSize: 12,
                    }}
                  >
                    <FileText size={14} /> TXT
                  </button>
                  <button
                    onClick={() => handleExport(selected.id, 'md')}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      padding: '6px 10px', borderRadius: 6,
                      background: 'var(--surface2)', border: '1px solid var(--border)',
                      color: 'var(--text-muted)', fontSize: 12,
                    }}
                  >
                    <FileCode size={14} /> MD
                  </button>
                  <button
                    onClick={() => handleExport(selected.id, 'json')}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      padding: '6px 10px', borderRadius: 6,
                      background: 'var(--surface2)', border: '1px solid var(--border)',
                      color: 'var(--text-muted)', fontSize: 12,
                    }}
                  >
                    <FileJson size={14} /> JSON
                  </button>
                  <button
                    onClick={() => handleDelete(selected.id)}
                    disabled={deleting}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      padding: '6px 10px', borderRadius: 6,
                      background: 'var(--red-dim)', border: '1px solid var(--red-dim)',
                      color: 'var(--red)', fontSize: 12,
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {/* Transcript */}
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--border)',
                borderRadius: 12, overflow: 'hidden', marginBottom: 16,
              }}>
                <div style={{
                  padding: '12px 16px', borderBottom: '1px solid var(--border)',
                  fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
                  textTransform: 'uppercase', letterSpacing: '0.05em',
                }}>
                  轉錄內容
                </div>
                <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                  {!selected.transcript || selected.transcript.length === 0 ? (
                    <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                      無轉錄內容
                    </div>
                  ) : (
                    selected.transcript.map((seg) => (
                      <div key={seg.id} style={{
                        padding: '10px 16px', borderBottom: '1px solid var(--border)',
                        fontSize: 13, lineHeight: 1.6,
                      }}>
                        <span style={{ color: 'var(--accent)', fontWeight: 600, marginRight: 8 }}>
                          {seg.speaker}
                        </span>
                        {seg.text}
                      </div>
                    ))
                  )}
                </div>
              </div>

              {/* Summary */}
              {selected.summary && (
                <div style={{
                  background: 'var(--surface)', border: '1px solid var(--border)',
                  borderRadius: 12, overflow: 'hidden',
                }}>
                  <div style={{
                    padding: '12px 16px', borderBottom: '1px solid var(--border)',
                    fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
                    textTransform: 'uppercase', letterSpacing: '0.05em',
                  }}>
                    會議摘要
                  </div>
                  <div style={{ padding: 16, fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                    {selected.summary}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
