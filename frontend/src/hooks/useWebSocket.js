import { useState, useEffect, useRef, useCallback } from 'react';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/meeting`;
const RECONNECT_DELAY = 3000;
const MAX_RECONNECTS = 5;
const PING_INTERVAL = 30000;

export default function useWebSocket() {
  const [isConnected, setIsConnected] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [transcript, setTranscript] = useState([]);
  const [chatMessages, setChatMessages] = useState([]);
  const [summary, setSummary] = useState('');
  const [error, setError] = useState(null);

  const wsRef = useRef(null);
  const reconnectCountRef = useRef(0);
  const pingTimerRef = useRef(null);
  const audioContextRef = useRef(null);
  const processorNodeRef = useRef(null);
  const sourceNodeRef = useRef(null);
  const streamRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setError(null);
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setIsConnected(true);
      reconnectCountRef.current = 0;
      pingTimerRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, PING_INTERVAL);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleMessage(msg);
      } catch {
        // binary or non-JSON message
      }
    };

    ws.onclose = () => {
      setIsConnected(false);
      clearInterval(pingTimerRef.current);
      if (reconnectCountRef.current < MAX_RECONNECTS) {
        reconnectCountRef.current += 1;
        setTimeout(connect, RECONNECT_DELAY);
      }
    };

    ws.onerror = () => {
      setError('WebSocket connection error');
    };
  }, []);

  const handleMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'ready':
        setSessionId(msg.session_id);
        break;
      case 'transcript':
        setTranscript((prev) => {
          const existing = prev.findIndex((s) => s.id === msg.segment.id);
          if (existing >= 0) {
            const next = [...prev];
            next[existing] = msg.segment;
            return next;
          }
          return [...prev, msg.segment];
        });
        break;
      case 'chat_stream_start':
        setChatMessages((prev) => [...prev, { role: 'assistant', content: '', message_id: msg.message_id, streaming: true }]);
        break;
      case 'chat_stream_delta':
        setChatMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.streaming) {
            last.content += msg.delta;
          }
          return next;
        });
        break;
      case 'chat_response':
        setChatMessages((prev) => {
          const next = [...prev];
          const idx = next.findIndex((m) => m.message_id === msg.message_id);
          if (idx >= 0) {
            next[idx] = { role: 'assistant', content: msg.answer, message_id: msg.message_id };
          }
          return next;
        });
        break;
      case 'chat_error':
        setChatMessages((prev) => [...prev, { role: 'system', content: `Error: ${msg.message}` }]);
        break;
      case 'summary_update':
        setSummary(msg.summary);
        break;
      case 'error':
        setError(msg.message);
        break;
      case 'stopped':
        setIsRecording(false);
        stopAudioCaptureRef();
        break;
      case 'pong':
        break;
      default:
        break;
    }
  }, []);

  // Ref to stopAudioCapture so handleMessage can use it
  const stopAudioCaptureRefFn = useCallback(() => {
    if (processorNodeRef.current) {
      processorNodeRef.current.port.postMessage({ type: 'drain' });
    }
    setTimeout(() => {
      processorNodeRef.current?.disconnect();
      sourceNodeRef.current?.disconnect();
      audioContextRef.current?.close().catch(() => {});
      streamRef.current?.getTracks().forEach((t) => t.stop());
      processorNodeRef.current = null;
      sourceNodeRef.current = null;
      audioContextRef.current = null;
      streamRef.current = null;
    }, 300);
  }, []);

  // Use a ref to allow handleMessage to call stopAudioCapture
  const stopAudioCaptureRef = useRef(stopAudioCaptureRefFn);
  stopAudioCaptureRef.current = stopAudioCaptureRefFn;

  const startMeeting = useCallback((language = '', asrPrompt = '') => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({
      type: 'start',
      language: language || undefined,
      asr_prompt: asrPrompt || undefined,
    }));
  }, []);

  const stopMeeting = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: 'stop' }));
    stopAudioCaptureRefFn();
    setIsRecording(false);
  }, [stopAudioCaptureRefFn]);

  const sendChat = useCallback((question) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    setChatMessages((prev) => [...prev, { role: 'user', content: question }]);
    wsRef.current.send(JSON.stringify({ type: 'chat', question }));
  }, []);

  const sendSummary = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: 'summary' }));
  }, []);

  const sendAudio = useCallback((audioData) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(audioData);
  }, []);

  const startAudioCapture = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const audioContext = new AudioContext({ sampleRate: 48000 });
      audioContextRef.current = audioContext;

      const workletPath = '/audio-worklet.js';
      await audioContext.audioWorklet.addModule(workletPath);
      await audioContext.resume();

      const source = audioContext.createMediaStreamSource(stream);
      sourceNodeRef.current = source;

      const processor = new AudioWorkletNode(audioContext, 'audio-capture-processor');
      processorNodeRef.current = processor;

      processor.port.onmessage = (e) => {
        if (e.data.type === 'audio_frame') {
          sendAudio(e.data.pcm);
        }
      };

      source.connect(processor);
      processor.connect(audioContext.destination);
      setIsRecording(true);
    } catch (e) {
      setError(`Microphone access failed: ${e.message}`);
    }
  }, [sendAudio]);

  const stopAudioCapture = useCallback(() => {
    stopAudioCaptureRefFn();
  }, [stopAudioCaptureRefFn]);

  const disconnect = useCallback(() => {
    clearInterval(pingTimerRef.current);
    stopAudioCaptureRefFn();
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, [stopAudioCaptureRefFn]);

  useEffect(() => {
    connect();
    return disconnect;
  }, [connect, disconnect]);

  return {
    isConnected,
    isRecording,
    sessionId,
    transcript,
    chatMessages,
    summary,
    error,
    connect,
    disconnect,
    startMeeting,
    stopMeeting,
    sendChat,
    sendSummary,
    startAudioCapture,
    stopAudioCapture,
    sendAudio,
  };
}
