# Meeting Realtime Voice — Pipeline Mermaid Diagrams

> 本文档描述当前单轨 realtime ASR pipeline。实时链路只保留 WebRTC VAD 分段后的 final transcript，不再存在 preview/final 双轨运行。

---

## 1. 系统全景图（High-Level Architecture）

```mermaid
flowchart TB
    subgraph CLIENT["Client (Browser)"]
        MIC["getUserMedia()"]
        PCM["PCM binary packets"]
        UI["Transcript / Chat UI"]
    end

    subgraph WS["WebSocket Layer"]
        SOCKET["/ws/meeting"]
        DECODER["AudioDecoder"]
        QUEUE["audio_queue"]
    end

    subgraph WORKERS["Async Workers"]
        AW["audio_worker()"]
        CHAT["handle_chat()"]
        SUM["handle_summary()"]
    end

    subgraph TRANSCRIBE["Realtime ASR"]
        VAD["WebRTCVADMeetingTranscriber"]
        ROUTER["FinalOnlyASRRouter"]
        ASR["ASRService"]
    end

    subgraph SESSION["Session / Persistence"]
        SESSION_OBJ["MeetingSession"]
        STORE["MeetingStore"]
        DB[("SQLite")]
        REC["SessionAudioRecorder"]
        WAV[("WAV file")]
    end

    MIC --> PCM --> SOCKET
    SOCKET --> DECODER --> QUEUE --> AW
    AW --> REC --> WAV
    AW --> VAD --> ROUTER --> ASR
    AW --> SESSION_OBJ --> STORE --> DB
    CHAT --> SESSION_OBJ
    SUM --> SESSION_OBJ
    SESSION_OBJ --> UI
    SOCKET --> CHAT
    SOCKET --> SUM
```

---

## 2. Audio Worker 流程（Audio Worker Flow）

```mermaid
flowchart TD
    START([audio_worker start])
    GET["audio_queue.get()"]
    EOS{"event_type == eos?"}
    RECORD["append_pcm()"]
    PUSH["transcriber.push_pcm()"]
    EVENTS{"final events returned?"}
    EMIT["emit_final_transcript_segment()"]
    DONE["send_transcribe_done(phase=final)"]
    FLUSH["transcriber.flush()"]
    FINALIZE["finalize recording + complete_session()"]
    STOP([stop])

    START --> GET --> EOS
    EOS -->|No| RECORD --> PUSH --> EVENTS
    EVENTS -->|Yes| EMIT --> DONE --> GET
    EVENTS -->|No| GET
    EOS -->|Yes| FLUSH --> EMIT --> DONE --> FINALIZE --> STOP
```

---

## 3. VAD 状态机（WebRTC VAD State Machine）

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> SpeechDetected: enter_speech_frames met
    SpeechDetected --> Idle: speech run not sustained
    SpeechDetected --> UtteranceActive: utterance opens
    UtteranceActive --> UtteranceActive: append speech PCM
    UtteranceActive --> SilenceDetected: silence starts
    SilenceDetected --> UtteranceActive: speech resumes
    SilenceDetected --> SealUtterance: endpoint silence reached
    UtteranceActive --> SealUtterance: max utterance reached
    SealUtterance --> QueueFinalTask: _seal_active()
    QueueFinalTask --> EmitFinalEvent: transcribe_final() completes
    EmitFinalEvent --> [*]
```

---

## 4. Final Transcript 持久化链路（Persistence Flow）

```mermaid
sequenceDiagram
    participant VAD as WebRTCVADMeetingTranscriber
    participant AW as audio_worker
    participant SESSION as MeetingSession
    participant STORE as MeetingStore
    participant UI as Browser UI

    VAD-->>AW: RealtimeTranscriptEvent(is_final=true)
    AW->>SESSION: add_transcript_segment()
    SESSION->>STORE: persist transcript row
    STORE-->>SESSION: persisted segment id
    AW-->>UI: {"type":"transcript","segment":{"id": "..."}}
    AW-->>UI: {"type":"transcribe_done","phase":"final"}
```

---

## 5. 摘要与问答依赖（Summary / QA Grounding）

```mermaid
flowchart LR
    FINAL["Persisted final transcript segments"] --> SESSION["MeetingSession.transcript"]
    SESSION --> SUMMARY["generate_summary()"]
    SESSION --> QA["handle_chat() / LLM context"]
    SUMMARY --> UI["Summary panel"]
    QA --> UI
```
