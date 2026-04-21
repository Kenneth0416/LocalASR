import { useState, useRef, useEffect } from 'react';
import useWebSocket from '../hooks/useWebSocket';
import Header from '../components/Header';
import TranscriptSegment from '../components/TranscriptSegment';
import ChatMessage from '../components/ChatMessage';
import { Mic, MicOff, Send, RefreshCw, BookOpen, MessageCircle, FileText } from 'lucide-react';

export default function MeetingRoom() {
  const ws = useWebSocket();
  const [chatInput, setChatInput] = useState('');
  const [language, setLanguage] = useState('');
  const transcriptRef = useRef(null);
  const chatRef = useRef(null);

  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [ws.transcript]);

  useEffect(() => {
    if (chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight;
    }
  }, [ws.chatMessages]);

  const handleStart = () => {
    ws.startMeeting(language, '');
    ws.startAudioCapture();
  };

  const handleStop = () => {
    ws.stopMeeting();
  };

  const handleChat = () => {
    if (!chatInput.trim()) return;
    ws.sendChat(chatInput.trim());
    setChatInput('');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="直播會議室">
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          style={{
            padding: '5px 10px', borderRadius: 7, border: '1px solid var(--border)',
            background: 'var(--surface2)', color: 'var(--text-muted)',
            fontSize: 12, fontFamily: 'inherit',
          }}
        >
          <option value="">自動偵測</option>
          <option value="Chinese">中文</option>
          <option value="English">English</option>
          <option value="Japanese">日本語</option>
          <option value="Korean">한국어</option>
        </select>

        {ws.isRecording ? (
          <button
            onClick={handleStop}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '6px 12px', borderRadius: 20,
              background: 'var(--red-dim)', color: 'var(--red)',
              border: '1px solid var(--red-dim)', fontSize: 12, fontWeight: 600,
            }}
          >
            <MicOff size={14} />
            結束會議
          </button>
        ) : (
          <button
            onClick={handleStart}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '6px 12px', borderRadius: 20,
              background: 'var(--accent)', color: 'white',
              border: 'none', fontSize: 12, fontWeight: 600,
            }}
          >
            <Mic size={14} />
            開始會議
          </button>
        )}
      </Header>

      {/* Three-column workspace */}
      <div style={{
        flex: 1, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr',
        gap: 1, background: 'var(--border)', overflow: 'hidden',
      }}>
        {/* Transcript Panel */}
        <div style={{ display: 'flex', flexDirection: 'column', background: 'var(--bg)', overflow: 'hidden' }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '12px 16px 10px', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
              <FileText size={12} style={{ display: 'inline', marginRight: 6, verticalAlign: 'middle' }} />
              轉錄文字
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{ws.transcript.length} 條</span>
          </div>
          <div ref={transcriptRef} style={{ flex: 1, overflowY: 'auto' }}>
            {ws.transcript.length === 0 ? (
              <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                {ws.isRecording ? '等待轉錄結果...' : '點擊「開始會議」開始轉錄'}
              </div>
            ) : (
              ws.transcript.map((seg) => (
                <TranscriptSegment
                  key={seg.id}
                  segment={seg}
                  sessionId={ws.sessionId}
                />
              ))
            )}
          </div>
        </div>

        {/* Chat Panel */}
        <div style={{ display: 'flex', flexDirection: 'column', background: 'var(--bg)', overflow: 'hidden' }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '12px 16px 10px', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
              <MessageCircle size={12} style={{ display: 'inline', marginRight: 6, verticalAlign: 'middle' }} />
              AI 問答
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{ws.chatMessages.length} 則</span>
          </div>
          <div ref={chatRef} style={{ flex: 1, overflowY: 'auto' }}>
            {ws.chatMessages.length === 0 ? (
              <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                會議中可隨時向 AI 提問
              </div>
            ) : (
              ws.chatMessages.map((msg, i) => (
                <ChatMessage key={i} message={msg} />
              ))
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
              style={{
                flex: 1, padding: '8px 12px', borderRadius: 8,
                border: '1px solid var(--border)', background: 'var(--surface2)',
                color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
              }}
            />
            <button
              onClick={handleChat}
              disabled={!chatInput.trim()}
              style={{
                padding: '8px 12px', borderRadius: 8,
                background: chatInput.trim() ? 'var(--accent)' : 'var(--surface2)',
                color: chatInput.trim() ? 'white' : 'var(--text-muted)',
                border: 'none',
              }}
            >
              <Send size={14} />
            </button>
          </div>
        </div>

        {/* Summary Panel */}
        <div style={{ display: 'flex', flexDirection: 'column', background: 'var(--bg)', overflow: 'hidden' }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '12px 16px 10px', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
              <BookOpen size={12} style={{ display: 'inline', marginRight: 6, verticalAlign: 'middle' }} />
              會議摘要
            </span>
            <button
              onClick={() => ws.sendSummary()}
              disabled={!ws.sessionId}
              style={{ color: 'var(--text-muted)', padding: 2 }}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
            {!ws.summary ? (
              <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                摘要將在轉錄完成後自動生成
              </div>
            ) : (
              <div style={{ fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                {ws.summary}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Error banner */}
      {ws.error && (
        <div style={{
          padding: '10px 16px', background: 'var(--red-dim)', color: 'var(--red)',
          fontSize: 12, borderTop: '1px solid var(--red-dim)',
        }}>
          {ws.error}
        </div>
      )}
    </div>
  );
}
