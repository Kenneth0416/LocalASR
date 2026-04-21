import { useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { usePost } from '../hooks/useApi';
import Header from '../components/Header';
import { UploadCloud, FileAudio, CheckCircle, X } from 'lucide-react';

export default function UploadPage() {
  const navigate = useNavigate();
  const { post, loading } = usePost();
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [result, setResult] = useState(null);
  const [language, setLanguage] = useState('');
  const [asrPrompt, setAsrPrompt] = useState('');

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped && dropped.type.startsWith('audio/')) {
      setFile(dropped);
      setResult(null);
    }
  }, []);

  const handleFileSelect = (e) => {
    const selected = e.target.files[0];
    if (selected) {
      setFile(selected);
      setResult(null);
    }
  };

  const handleUpload = async () => {
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    if (language) formData.append('language', language);
    if (asrPrompt) formData.append('asr_prompt', asrPrompt);

    try {
      const data = await post('/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      setResult(data);
    } catch {
      // error handled by interceptor
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="上傳轉錄" />

      <div style={{ flex: 1, overflowY: 'auto', padding: 24, maxWidth: 800, margin: '0 auto', width: '100%' }}>
        {/* Dropzone */}
        {!result && (
          <>
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              style={{
                border: `2px dashed ${dragOver ? 'var(--accent)' : 'var(--border)'}`,
                borderRadius: 16, padding: 48,
                textAlign: 'center', cursor: 'pointer',
                background: dragOver ? 'var(--accent-dim)' : 'var(--surface)',
                transition: 'all 0.2s',
              }}
            >
              <input
                type="file"
                accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac"
                onChange={handleFileSelect}
                style={{ display: 'none' }}
                id="audio-upload"
              />
              <label htmlFor="audio-upload" style={{ cursor: 'pointer', display: 'block' }}>
                <UploadCloud size={40} style={{ color: 'var(--accent)', marginBottom: 16 }} />
                <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>
                  拖放音頻文件到這裡，或點擊選擇
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  支援 WAV、MP3、M4A、OGG、FLAC，最大 500MB
                </div>
              </label>
            </div>

            {file && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 12,
                marginTop: 16, padding: '12px 16px',
                background: 'var(--surface)', border: '1px solid var(--border)',
                borderRadius: 10,
              }}>
                <FileAudio size={20} style={{ color: 'var(--accent)' }} />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 500 }}>{file.name}</div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    {(file.size / 1024 / 1024).toFixed(2)} MB
                  </div>
                </div>
                <button onClick={() => setFile(null)} style={{ color: 'var(--text-muted)' }}>
                  <X size={16} />
                </button>
              </div>
            )}

            {/* Options */}
            {file && (
              <div style={{ marginTop: 16, display: 'flex', gap: 12 }}>
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  style={{
                    padding: '8px 12px', borderRadius: 8,
                    border: '1px solid var(--border)', background: 'var(--surface2)',
                    color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
                  }}
                >
                  <option value="">自動偵測語言</option>
                  <option value="Chinese">中文</option>
                  <option value="English">English</option>
                  <option value="Japanese">日本語</option>
                  <option value="Korean">한국어</option>
                </select>
                <input
                  value={asrPrompt}
                  onChange={(e) => setAsrPrompt(e.target.value)}
                  placeholder="ASR 提示詞（選填）：專有名詞、人名等"
                  style={{
                    flex: 1, padding: '8px 12px', borderRadius: 8,
                    border: '1px solid var(--border)', background: 'var(--surface2)',
                    color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
                  }}
                />
              </div>
            )}

            {file && (
              <button
                onClick={handleUpload}
                disabled={loading}
                style={{
                  marginTop: 16, width: '100%', padding: '12px',
                  borderRadius: 10, background: loading ? 'var(--surface2)' : 'var(--accent)',
                  color: 'white', fontSize: 14, fontWeight: 600,
                  border: 'none', cursor: loading ? 'not-allowed' : 'pointer',
                }}
              >
                {loading ? '處理中...' : '開始轉錄'}
              </button>
            )}
          </>
        )}

        {/* Result */}
        {result && (
          <div style={{
            background: 'var(--surface)', border: '1px solid var(--border)',
            borderRadius: 12, overflow: 'hidden',
          }}>
            <div style={{
              padding: '16px 20px', borderBottom: '1px solid var(--border)',
              display: 'flex', alignItems: 'center', gap: 12,
            }}>
              <CheckCircle size={20} style={{ color: 'var(--green)' }} />
              <div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>轉錄完成</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  {result.segments?.length || 0} 段文字 · {result.audio_duration?.toFixed(1) || 0} 秒
                </div>
              </div>
            </div>

            <div style={{ maxHeight: 400, overflowY: 'auto' }}>
              {result.segments?.map((seg) => (
                <div key={seg.id} style={{
                  padding: '10px 16px', borderBottom: '1px solid var(--border)',
                  fontSize: 13, lineHeight: 1.6,
                }}>
                  <span style={{ color: 'var(--text-muted)', fontSize: 11, marginRight: 8 }}>
                    {seg.speaker}
                  </span>
                  {seg.text}
                </div>
              ))}
            </div>

            <div style={{
              padding: '12px 16px', borderTop: '1px solid var(--border)',
              display: 'flex', gap: 8,
            }}>
              <button
                onClick={() => navigate('/library')}
                style={{
                  padding: '8px 16px', borderRadius: 8,
                  background: 'var(--accent)', color: 'white',
                  fontSize: 13, fontWeight: 600, border: 'none',
                }}
              >
                前往紀錄庫
              </button>
              <button
                onClick={() => { setResult(null); setFile(null); }}
                style={{
                  padding: '8px 16px', borderRadius: 8,
                  background: 'var(--surface2)', color: 'var(--text)',
                  fontSize: 13, border: '1px solid var(--border)',
                }}
              >
                上傳新文件
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
