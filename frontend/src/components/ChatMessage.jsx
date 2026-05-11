import { User, Bot } from 'lucide-react';
import MarkdownRenderer from './MarkdownRenderer';

export default function ChatMessage({ message }) {
  const isUser = message.role === 'user';

  return (
    <div style={{
      display: 'flex', gap: 10, alignItems: 'flex-start',
      padding: '10px 14px',
    }}>
      <div style={{
        width: 26, height: 26, borderRadius: '50%',
        background: isUser ? 'var(--surface2)' : 'var(--accent-dim)',
        color: isUser ? 'var(--text-muted)' : 'var(--accent)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        flexShrink: 0,
      }}>
        {isUser ? <User size={14} /> : <Bot size={14} />}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 4 }}>
          {isUser ? 'You' : 'AI Assistant'}
        </div>
        {isUser ? (
          <div style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--text)', whiteSpace: 'pre-wrap' }}>
            {message.content}
          </div>
        ) : (
          <MarkdownRenderer>{message.content}</MarkdownRenderer>
        )}
      </div>
    </div>
  );
}
