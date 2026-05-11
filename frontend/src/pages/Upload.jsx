import { useState, useCallback, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api/client';
import Header from '../components/Header';
import { UploadCloud, FileAudio, CheckCircle, X, Mic, Wand2 } from 'lucide-react';

// ── CSS animations injected once ──────────────────────────────────────────────
const STYLES = `
@keyframes waveBar {
  0%, 100% { transform: scaleY(0.25); opacity: 0.5; }
  50%       { transform: scaleY(1);    opacity: 1;   }
}
@keyframes shimmer {
  0%   { background-position: -300% center; }
  100% { background-position:  300% center; }
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes fadeSlideIn {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0);   }
}
@keyframes pulse {
  0%, 100% { opacity: 1;   transform: scale(1);    }
  50%       { opacity: 0.6; transform: scale(0.94); }
}
@keyframes stepDone {
  0%   { transform: scale(0.6); opacity: 0; }
  60%  { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(1);   opacity: 1; }
}
`;

function injectStyles() {
  if (document.getElementById('upload-anim-styles')) return;
  const el = document.createElement('style');
  el.id = 'upload-anim-styles';
  el.textContent = STYLES;
  document.head.appendChild(el);
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function WaveformBars({ color = 'var(--accent)' }) {
  const delays = [0, 0.15, 0.3, 0.45, 0.6, 0.45, 0.3, 0.15, 0];
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, height: 40 }}>
      {delays.map((delay, i) => (
        <div key={i} style={{
          width: 4, height: 40, borderRadius: 2,
          background: color, transformOrigin: 'bottom',
          animation: `waveBar 1.1s ease-in-out ${delay}s infinite`,
        }} />
      ))}
    </div>
  );
}

function StepIndicator({ steps, current }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', marginBottom: 32 }}>
      {steps.map((step, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', flex: i < steps.length - 1 ? 1 : 'none' }}>
            {/* Circle */}
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
              <div style={{
                width: 36, height: 36, borderRadius: '50%',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 13, fontWeight: 700,
                background: done ? 'var(--green, #34d399)' : active ? 'var(--accent)' : 'var(--surface2)',
                color: done || active ? 'white' : 'var(--text-muted)',
                border: `2px solid ${done ? 'var(--green, #34d399)' : active ? 'var(--accent)' : 'var(--border)'}`,
                transition: 'all 0.3s ease',
                animation: done ? 'stepDone 0.35s ease' : active ? 'pulse 2s ease-in-out infinite' : 'none',
                boxShadow: active ? '0 0 0 4px var(--accent-dim)' : 'none',
              }}>
                {done ? <CheckCircle size={16} /> : <span>{i + 1}</span>}
              </div>
              <span style={{
                fontSize: 11, fontWeight: 500, whiteSpace: 'nowrap',
                color: done ? 'var(--green, #34d399)' : active ? 'var(--accent)' : 'var(--text-muted)',
                transition: 'color 0.3s ease',
              }}>
                {step}
              </span>
            </div>
            {/* Connector */}
            {i < steps.length - 1 && (
              <div style={{
                flex: 1, height: 2, margin: '0 8px', marginBottom: 22,
                background: done ? 'var(--green, #34d399)' : 'var(--border)',
                transition: 'background 0.4s ease',
              }} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function UploadProgressCard({ progress, filename, fileSize }) {
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 16, padding: 28, animation: 'fadeSlideIn 0.3s ease',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <UploadCloud size={20} style={{ color: 'var(--accent)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {filename}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fileSize} MB</div>
        </div>
        <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--accent)', fontVariantNumeric: 'tabular-nums' }}>
          {progress}%
        </span>
      </div>

      {/* Progress bar */}
      <div style={{
        height: 6, borderRadius: 3,
        background: 'var(--surface2)', overflow: 'hidden',
      }}>
        <div style={{
          height: '100%', borderRadius: 3,
          width: `${progress}%`,
          background: progress < 100
            ? 'linear-gradient(90deg, var(--accent), #818cf8, var(--accent))'
            : 'var(--green, #34d399)',
          backgroundSize: '200% auto',
          animation: progress < 100 ? 'shimmer 1.8s linear infinite' : 'none',
          transition: 'width 0.2s ease, background 0.4s ease',
        }} />
      </div>

      <div style={{ marginTop: 10, fontSize: 11, color: 'var(--text-muted)' }}>
        {progress < 100 ? '正在上傳音頻文件...' : '上傳完成，準備轉錄...'}
      </div>
    </div>
  );
}

function TranscribingCard({ filename, elapsed }) {
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  const timeStr = mins > 0 ? `${mins}:${String(secs).padStart(2, '0')}` : `${secs}s`;

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--accent)',
      borderRadius: 16, padding: 28,
      boxShadow: '0 0 0 1px var(--accent-dim)',
      animation: 'fadeSlideIn 0.3s ease',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Mic size={18} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text)' }}>AI 轉錄中</span>
        </div>
        <div style={{
          fontSize: 13, fontWeight: 600, color: 'var(--accent)',
          fontVariantNumeric: 'tabular-nums',
          background: 'var(--accent-dim)', padding: '3px 10px', borderRadius: 20,
        }}>
          {timeStr}
        </div>
      </div>

      {/* Waveform visualization */}
      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 20 }}>
        <WaveformBars />
      </div>

      {/* File info */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '8px 12px', borderRadius: 8,
        background: 'var(--surface2)',
        fontSize: 12, color: 'var(--text-muted)',
      }}>
        <FileAudio size={14} style={{ flexShrink: 0 }} />
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{filename}</span>
      </div>

      {/* Status messages cycling */}
      <TranscribeStatusMessages elapsed={elapsed} />
    </div>
  );
}

const STATUS_MESSAGES = [
  '語音活動偵測中...',
  '音頻分段處理中...',
  'AI 模型推理中...',
  '語言識別中...',
  '文字後處理中...',
];

function TranscribeStatusMessages({ elapsed }) {
  const idx = Math.floor(elapsed / 7) % STATUS_MESSAGES.length;
  return (
    <div style={{
      marginTop: 14, textAlign: 'center',
      fontSize: 12, color: 'var(--text-muted)',
      minHeight: 18,
    }}>
      <span key={idx} style={{ animation: 'fadeSlideIn 0.4s ease' }}>
        {STATUS_MESSAGES[idx]}
      </span>
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────────

const STEPS = ['選擇文件', '上傳', '轉錄中', '完成'];

// phase: 'idle' | 'uploading' | 'polling' | 'done' | 'error'
const PHASE_STEP = { idle: 0, uploading: 1, polling: 2, done: 3, error: 1 };

export default function UploadPage() {
  injectStyles();
  const navigate = useNavigate();
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [phase, setPhase] = useState('idle');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [taskId, setTaskId] = useState(null);
  const [language, setLanguage] = useState('');
  const [asrPrompt, setAsrPrompt] = useState('');
  const elapsedRef = useRef(null);
  const transcribeStartRef = useRef(null);

  // Elapsed timer while polling
  useEffect(() => {
    if (phase === 'uploading' || phase === 'polling') {
      if (phase === 'polling' && !transcribeStartRef.current) {
        transcribeStartRef.current = Date.now();
      }
      elapsedRef.current = setInterval(() => {
        if (transcribeStartRef.current) {
          setElapsed(Math.floor((Date.now() - transcribeStartRef.current) / 1000));
        }
      }, 1000);
    } else {
      clearInterval(elapsedRef.current);
    }
    return () => clearInterval(elapsedRef.current);
  }, [phase]);

  // Resume active upload task from localStorage on mount
  useEffect(() => {
    const savedTaskId = localStorage.getItem('activeUploadTaskId');
    if (savedTaskId) {
      api.get(`/upload/${savedTaskId}/status`)
        .then((res) => {
          const data = res.data;
          if (data.status === 'completed') {
            setResult(data.result);
            setPhase('done');
            localStorage.removeItem('activeUploadTaskId');
          } else if (data.status === 'failed') {
            setError(data.error || '轉錄失敗');
            setPhase('error');
            localStorage.removeItem('activeUploadTaskId');
          } else {
            // Still processing — resume polling
            setTaskId(savedTaskId);
            transcribeStartRef.current = Date.now();
            setPhase('polling');
          }
        })
        .catch(() => {
          localStorage.removeItem('activeUploadTaskId');
        });
    }
  }, []);

  // Poll for upload task status
  useEffect(() => {
    if (phase !== 'polling' || !taskId) return;

    let cancelled = false;
    let slowTimer = null;

    const poll = async () => {
      try {
        const res = await api.get(`/upload/${taskId}/status`);
        if (cancelled) return;
        const data = res.data;

        if (data.status === 'completed') {
          setResult(data.result);
          setPhase('done');
          localStorage.removeItem('activeUploadTaskId');
        } else if (data.status === 'failed') {
          setError(data.error || '轉錄失敗');
          setPhase('error');
          localStorage.removeItem('activeUploadTaskId');
        }
      } catch (err) {
        if (!cancelled && err?.response?.status === 404) {
          setError('上傳任務已過期，請重新上傳');
          setPhase('error');
          localStorage.removeItem('activeUploadTaskId');
        }
      }
    };

    // Poll immediately, then every 3s
    poll();
    const interval = setInterval(poll, 3000);

    // Switch to 5s after 30s
    const switchToSlow = setTimeout(() => {
      clearInterval(interval);
      if (!cancelled) slowTimer = setInterval(poll, 5000);
    }, 30000);

    return () => {
      cancelled = true;
      clearInterval(interval);
      clearTimeout(switchToSlow);
      if (slowTimer) clearInterval(slowTimer);
    };
  }, [phase, taskId]);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped) { setFile(dropped); setResult(null); setError(null); setPhase('idle'); }
  }, []);

  const handleFileSelect = (e) => {
    const selected = e.target.files[0];
    if (selected) { setFile(selected); setResult(null); setError(null); setPhase('idle'); }
  };

  const handleUpload = async () => {
    if (!file) return;
    setError(null);
    setUploadProgress(0);
    setElapsed(0);
    transcribeStartRef.current = null;
    setPhase('uploading');

    const formData = new FormData();
    formData.append('file', file);
    if (language) formData.append('language', language);
    if (asrPrompt) formData.append('asr_prompt', asrPrompt);

    try {
      const res = await api.post('/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 60000,  // 60s for file upload (no longer waiting for transcription)
        onUploadProgress: (e) => {
          const pct = Math.round((e.loaded / e.total) * 100);
          setUploadProgress(pct);
        },
      });

      // Server returned task_id — start polling
      const { task_id } = res.data;
      setTaskId(task_id);
      localStorage.setItem('activeUploadTaskId', task_id);
      transcribeStartRef.current = Date.now();
      setPhase('polling');

    } catch (err) {
      setError(err?.response?.data?.detail || '上傳失敗，請重試');
      setPhase('error');
    }
  };

  const reset = () => {
    setFile(null); setResult(null); setError(null);
    setPhase('idle'); setUploadProgress(0); setElapsed(0);
    transcribeStartRef.current = null;
  };

  const isProcessing = phase === 'uploading' || phase === 'polling';
  const currentStep = PHASE_STEP[phase] ?? 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="上傳轉錄" />

      <div style={{ flex: 1, overflowY: 'auto', padding: 24, maxWidth: 680, margin: '0 auto', width: '100%' }}>

        {/* Step indicator — always visible once a file is selected or processing */}
        {(file || isProcessing || phase === 'done') && (
          <StepIndicator steps={STEPS} current={currentStep} />
        )}

        {/* ── IDLE: dropzone + options ── */}
        {phase === 'idle' && (
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
              <>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 12,
                  marginTop: 16, padding: '12px 16px',
                  background: 'var(--surface)', border: '1px solid var(--border)',
                  borderRadius: 10, animation: 'fadeSlideIn 0.25s ease',
                }}>
                  <FileAudio size={20} style={{ color: 'var(--accent)' }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {(file.size / 1024 / 1024).toFixed(2)} MB
                    </div>
                  </div>
                  <button onClick={reset} style={{ color: 'var(--text-muted)', background: 'none', border: 'none', cursor: 'pointer' }}>
                    <X size={16} />
                  </button>
                </div>

                <div style={{ marginTop: 12, display: 'flex', gap: 12 }}>
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

                <button
                  onClick={handleUpload}
                  style={{
                    marginTop: 16, width: '100%', padding: '13px',
                    borderRadius: 10, background: 'var(--accent)',
                    color: 'white', fontSize: 14, fontWeight: 600,
                    border: 'none', cursor: 'pointer',
                    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
                  }}
                >
                  <Wand2 size={16} />
                  開始轉錄
                </button>
              </>
            )}
          </>
        )}

        {/* ── UPLOADING ── */}
        {phase === 'uploading' && (
          <UploadProgressCard
            progress={uploadProgress}
            filename={file?.name}
            fileSize={(file?.size / 1024 / 1024).toFixed(2)}
          />
        )}

        {/* ── POLLING (transcription in progress) ── */}
        {phase === 'polling' && (
          <TranscribingCard filename={file?.name} elapsed={elapsed} />
        )}

        {/* ── ERROR ── */}
        {phase === 'error' && (
          <div style={{
            background: 'var(--red-dim, rgba(239,68,68,0.1))',
            border: '1px solid var(--red, #ef4444)',
            borderRadius: 12, padding: 20,
            animation: 'fadeSlideIn 0.3s ease',
          }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--red, #ef4444)', marginBottom: 8 }}>
              轉錄失敗
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>{error}</div>
            <button
              onClick={reset}
              style={{
                padding: '8px 16px', borderRadius: 8,
                background: 'var(--surface2)', color: 'var(--text)',
                fontSize: 13, border: '1px solid var(--border)', cursor: 'pointer',
              }}
            >
              重新上傳
            </button>
          </div>
        )}

        {/* ── DONE: result ── */}
        {phase === 'done' && result && (
          <div style={{
            background: 'var(--surface)', border: '1px solid var(--border)',
            borderRadius: 12, overflow: 'hidden',
            animation: 'fadeSlideIn 0.4s ease',
          }}>
            <div style={{
              padding: '16px 20px', borderBottom: '1px solid var(--border)',
              display: 'flex', alignItems: 'center', gap: 12,
              background: 'var(--green-dim, rgba(52,211,153,0.08))',
            }}>
              <CheckCircle size={20} style={{ color: 'var(--green, #34d399)' }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, fontWeight: 600 }}>轉錄完成</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  {result.segments?.length || 0} 段文字 · {result.audio_duration?.toFixed(1) || 0} 秒音頻
                  {result.processing_time ? ` · 耗時 ${result.processing_time.toFixed(1)}s` : ''}
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
                  fontSize: 13, fontWeight: 600, border: 'none', cursor: 'pointer',
                }}
              >
                前往紀錄庫
              </button>
              <button
                onClick={reset}
                style={{
                  padding: '8px 16px', borderRadius: 8,
                  background: 'var(--surface2)', color: 'var(--text)',
                  fontSize: 13, border: '1px solid var(--border)', cursor: 'pointer',
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
