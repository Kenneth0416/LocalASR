import { useNavigate } from 'react-router-dom';
import { useGet } from '../hooks/useApi';
import Header from '../components/Header';
import MeetingCard from '../components/MeetingCard';
import EmptyState from '../components/EmptyState';
import { Mic, Upload, Library, Activity, Clock, MessageSquare, Zap } from 'lucide-react';

export default function Dashboard() {
  const navigate = useNavigate();
  const { data: health, loading: healthLoading } = useGet('/health');
  const { data: meetingsData, loading: meetingsLoading } = useGet('/meetings');

  const meetings = meetingsData?.meetings || [];
  const recentMeetings = meetings.slice(0, 5);

  const stats = [
    { label: '本月會議', value: meetings.length.toString(), sub: '場', icon: Activity },
    { label: '總錄音時長', value: '0', sub: '小時', icon: Clock },
    { label: 'AI 問答', value: '0', sub: '次', icon: MessageSquare },
    { label: '系統狀態', value: health?.status === 'ok' ? '正常' : '異常', sub: '', icon: Zap },
  ];

  const quickActions = [
    { icon: Mic, title: '開始新會議', sub: '即時轉錄 + AI 摘要', path: '/live', color: 'var(--accent-dim)' },
    { icon: Upload, title: '上傳音頻', sub: '離線轉錄處理', path: '/upload', color: 'var(--green-dim)' },
    { icon: Library, title: '會議紀錄庫', sub: '瀏覽所有歷史記錄', path: '/library', color: 'var(--yellow-dim)' },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="首頁" />

      <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
        {/* Stats */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 24 }}>
          {stats.map((s) => (
            <div key={s.label} style={{
              background: 'var(--surface)', border: '1px solid var(--border)',
              borderRadius: 12, padding: 18,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                <s.icon size={16} style={{ color: 'var(--accent)' }} />
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{s.label}</span>
              </div>
              <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text)' }}>
                {s.value}
              </div>
              {s.sub && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{s.sub}</div>}
            </div>
          ))}
        </div>

        {/* Quick Actions */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, marginBottom: 24 }}>
          {quickActions.map((qa) => (
            <div
              key={qa.title}
              onClick={() => navigate(qa.path)}
              style={{
                background: 'var(--surface)', border: '1px solid var(--border)',
                borderRadius: 10, padding: 16, cursor: 'pointer',
                transition: 'all 0.2s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = 'var(--accent)';
                e.currentTarget.style.transform = 'translateY(-1px)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = 'var(--border)';
                e.currentTarget.style.transform = 'translateY(0)';
              }}
            >
              <div style={{
                width: 32, height: 32, borderRadius: 8,
                background: qa.color, display: 'flex', alignItems: 'center', justifyContent: 'center',
                marginBottom: 8,
              }}>
                <qa.icon size={16} style={{ color: 'var(--accent)' }} />
              </div>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>{qa.title}</div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{qa.sub}</div>
            </div>
          ))}
        </div>

        {/* Recent Meetings */}
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 12, overflow: 'hidden',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '14px 16px', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>最近會議</span>
            <button
              onClick={() => navigate('/library')}
              style={{ fontSize: 12, color: 'var(--accent)' }}
            >
              查看全部 →
            </button>
          </div>

          {meetingsLoading ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              載入中...
            </div>
          ) : recentMeetings.length === 0 ? (
            <EmptyState
              title="尚無會議記錄"
              subtitle="開始一場新會議或上傳音頻文件"
              action={
                <button
                  onClick={() => navigate('/live')}
                  style={{
                    marginTop: 8, padding: '8px 16px', borderRadius: 8,
                    background: 'var(--accent)', color: 'white',
                    fontSize: 13, fontWeight: 600, border: 'none',
                  }}
                >
                  開始新會議
                </button>
              }
            />
          ) : (
            recentMeetings.map((m) => (
              <MeetingCard
                key={m.id}
                meeting={m}
                onClick={() => navigate('/library')}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
