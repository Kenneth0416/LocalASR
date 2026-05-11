import { useState, useEffect } from 'react';
import { useGet } from '../hooks/useApi';
import api from '../api/client';
import Header from '../components/Header';
import MeetingCard from '../components/MeetingCard';
import EmptyState from '../components/EmptyState';
import StatusBadge from '../components/StatusBadge';
import { Search, Trash2, FileText, FileJson, FileCode, Download, Send, User, Bot } from 'lucide-react';
import TemplateSelector from '../components/TemplateSelector';
import MarkdownRenderer from '../components/MarkdownRenderer';

export default function Library() {
  const { data, loading, refetch } = useGet('/meetings');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [selectedTemplate, setSelectedTemplate] = useState('preset-standard');
  const [regenerating, setRegenerating] = useState(false);
  const [chatInput, setChatInput] = useState('');
  const [chatHistory, setChatHistory] = useState([]);
  const [chatLoading, setChatLoading] = useState(false);

  // Refetch meeting list when navigating back to this page
  useEffect(() => {
    refetch();
  }, []);

  const handleSelect = async (m) => {
    setSelected(m);
    setDetailLoading(true);
    try {
      const detail = await api.get(`/meetings/${m.session_id}`);
      setSelected(detail.data);
      setChatHistory(detail.data.chat_history || []);
    } catch {
      // keep the list-level data
    } finally {
      setDetailLoading(false);
    }
  };

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
      if (selected?.id === id) {
        setSelected(null);
        setChatHistory([]);
      }
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

  const handleRegenerateSummary = async () => {
    if (!selected?.id) return;
    setRegenerating(true);
    try {
      const res = await api.post(`/meetings/${selected.id}/summary`, {
        template_id: selectedTemplate,
      }, { timeout: 300000 });
      setSelected(res.data);
    } catch {
      // error handled by interceptor
    } finally {
      setRegenerating(false);
    }
  };

  const handleChat = async () => {
    if (!chatInput.trim() || !selected?.id) return;
    const question = chatInput.trim();
    setChatInput('');
    setChatHistory((prev) => [...prev, { role: 'user', content: question }]);
    setChatLoading(true);
    // Add placeholder assistant message for streaming
    setChatHistory((prev) => [...prev, { role: 'assistant', content: '', streaming: true }]);
    try {
      const res = await fetch(`/api/meetings/${selected.id}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line in buffer
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = JSON.parse(line.slice(6));
          if (payload.delta) {
            setChatHistory((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.streaming) {
                next[next.length - 1] = { ...last, content: last.content + payload.delta };
              }
              return next;
            });
          } else if (payload.done) {
            setChatHistory((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.streaming) {
                next[next.length - 1] = { role: 'assistant', content: payload.answer };
              }
              return next;
            });
          } else if (payload.error) {
            throw new Error(payload.error);
          }
        }
      }
    } catch {
      setChatHistory((prev) => {
        // Remove the streaming placeholder if it's still there
        const filtered = prev.filter((m) => !m.streaming);
        return [...filtered, { role: 'system', content: 'AI 回答失敗，請重試' }];
      });
    } finally {
      setChatLoading(false);
    }
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
                key={m.session_id}
                meeting={m}
                onClick={() => handleSelect(m)}
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
          ) : detailLoading ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)' }}>載入中...</div>
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
                  {selected.recording_path && (
                    <button
                      onClick={() => window.open(`/api/meetings/${selected.id}/recording`, '_blank')}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 4,
                        padding: '6px 10px', borderRadius: 6,
                        background: 'var(--surface2)', border: '1px solid var(--border)',
                        color: 'var(--text-muted)', fontSize: 12,
                      }}
                    >
                      <Download size={14} /> 錄音
                    </button>
                  )}
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
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--border)',
                borderRadius: 12, overflow: 'hidden',
              }}>
                <div style={{
                  padding: '12px 16px', borderBottom: '1px solid var(--border)',
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                  <span style={{
                    fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
                    textTransform: 'uppercase', letterSpacing: '0.05em',
                  }}>
                    會議摘要
                  </span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <TemplateSelector
                      value={selectedTemplate}
                      onChange={setSelectedTemplate}
                    />
                    <button
                      onClick={handleRegenerateSummary}
                      disabled={regenerating}
                      style={{
                        padding: '4px 10px', borderRadius: 6,
                        background: 'var(--accent)', color: 'white',
                        border: 'none', fontSize: 11, fontWeight: 600,
                        opacity: regenerating ? 0.6 : 1,
                      }}
                    >
                      {regenerating ? '生成中...' : '重新生成'}
                    </button>
                  </div>
                </div>
                {selected.summary ? (
                  <div style={{ padding: 16 }}>
                    <MarkdownRenderer>{selected.summary}</MarkdownRenderer>
                  </div>
                ) : (
                  <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                    尚無摘要，點擊「重新生成」生成摘要
                  </div>
                )}
              </div>

              {/* Chat */}
              <div style={{
                background: 'var(--surface)', border: '1px solid var(--border)',
                borderRadius: 12, overflow: 'hidden', marginTop: 16,
              }}>
                <div style={{
                  padding: '12px 16px', borderBottom: '1px solid var(--border)',
                  fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
                  textTransform: 'uppercase', letterSpacing: '0.05em',
                }}>
                  AI 問答{chatHistory.length > 0 ? ` · ${chatHistory.length} 則` : ''}
                </div>
                <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                  {chatHistory.length === 0 ? (
                    <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                      向 AI 提問關於這場會議的問題
                    </div>
                  ) : (
                    chatHistory.map((msg, i) => (
                      <div key={i} style={{
                        display: 'flex', gap: 10, alignItems: 'flex-start',
                        padding: '10px 16px', borderBottom: i < chatHistory.length - 1 ? '1px solid var(--border)' : 'none',
                      }}>
                        <div style={{
                          width: 26, height: 26, borderRadius: '50%',
                          background: msg.role === 'user' ? 'var(--surface2)' : 'var(--accent-dim)',
                          color: msg.role === 'user' ? 'var(--text-muted)' : 'var(--accent)',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          flexShrink: 0,
                        }}>
                          {msg.role === 'user' ? <User size={14} /> : <Bot size={14} />}
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 4 }}>
                            {msg.role === 'user' ? 'You' : msg.role === 'assistant' ? 'AI Assistant' : 'System'}
                          </div>
                          {msg.role === 'assistant' ? (
                            <div>
                              <MarkdownRenderer>{msg.content}</MarkdownRenderer>
                              {msg.streaming && <span style={{ display: 'inline-block', width: 6, height: 14, background: 'var(--accent)', marginLeft: 2, verticalAlign: 'text-bottom', animation: 'blink 1s step-end infinite' }} />}
                            </div>
                          ) : (
                            <div style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--text)', whiteSpace: 'pre-wrap' }}>
                              {msg.content}
                            </div>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                  {chatLoading && !chatHistory.some((m) => m.streaming) && (
                    <div style={{ padding: '10px 16px', fontSize: 13, color: 'var(--text-muted)' }}>
                      AI 思考中...
                    </div>
                  )}
                </div>
                <div style={{
                  display: 'flex', gap: 8, padding: '10px 14px',
                  borderTop: '1px solid var(--border)',
                }}>
                  <input
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleChat()}
                    placeholder="輸入問題，按 Enter 發送..."
                    disabled={chatLoading}
                    style={{
                      flex: 1, padding: '8px 12px', borderRadius: 8,
                      border: '1px solid var(--border)', background: 'var(--surface2)',
                      color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
                    }}
                  />
                  <button
                    onClick={handleChat}
                    disabled={!chatInput.trim() || chatLoading}
                    style={{
                      padding: '8px 12px', borderRadius: 8,
                      background: chatInput.trim() && !chatLoading ? 'var(--accent)' : 'var(--surface2)',
                      color: chatInput.trim() && !chatLoading ? 'white' : 'var(--text-muted)',
                      border: 'none',
                    }}
                  >
                    <Send size={14} />
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
