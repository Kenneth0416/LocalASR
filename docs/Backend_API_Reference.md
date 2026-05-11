# MeetScribe Backend API Reference

> 最后更新: 2026-04-22 | 框架: FastAPI | 传输: HTTP REST + WebSocket

---

## 目录

1. [概览](#1-概览)
2. [基础信息](#2-基础信息)
3. [健康检查与监控](#3-健康检查与监控)
4. [文件上传转写](#4-文件上传转写)
5. [会议历史管理](#5-会议历史管理)
6. [会议导出](#6-会议导出)
7. [会议录音下载](#7-会议录音下载)
8. [转写段落编辑](#8-转写段落编辑)
9. [会议摘要](#9-会议摘要)
10. [会议聊天 (HTTP)](#10-会议聊天-http)
11. [活跃会话查询](#11-活跃会话查询)
12. [WebSocket 实时转写](#12-websocket-实时转写)
13. [数据结构定义](#13-数据结构定义)
14. [错误处理](#14-错误处理)
15. [CORS 与安全](#15-cors-与安全)

---

## 1. 概览

MeetScribe 后端提供两类接口:

| 类型 | 用途 | 协议 |
|------|------|------|
| **REST API** | 文件上传、会议管理、导出、聊天 | HTTP/HTTPS |
| **WebSocket** | 实时音频流转写、实时聊天、实时摘要 | WS/WSS |

### 端点总表

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 进程存活检查 |
| `GET` | `/api/readiness` | 依赖就绪检查 |
| `GET` | `/api/pipeline` | Pipeline 深度调试 |
| `POST` | `/api/upload` | 上传音频文件转写 |
| `GET` | `/api/meetings` | 会议列表 |
| `GET` | `/api/meetings/{session_id}` | 会议详情 |
| `DELETE` | `/api/meetings/{session_id}` | 删除会议 |
| `GET` | `/api/meetings/{session_id}/export.txt` | 导出纯文本 |
| `GET` | `/api/meetings/{session_id}/export.md` | 导出 Markdown |
| `GET` | `/api/meetings/{session_id}/export.json` | 导出 JSON |
| `GET` | `/api/meetings/{session_id}/recording` | 下载录音 WAV |
| `PATCH` | `/api/meetings/{session_id}/transcript/{segment_id}` | 编辑转写段落 |
| `POST` | `/api/meetings/{session_id}/summary` | 重新生成摘要 |
| `POST` | `/api/meetings/{session_id}/chat` | HTTP 聊天 |
| `GET` | `/api/sessions` | 活跃会话列表 (需启用) |
| `WS` | `/ws/meeting` | 实时音频转写 WebSocket |

---

## 2. 基础信息

### Base URL

```
http://{HOST}:{PORT}
```

默认: `http://127.0.0.1:8800`

### 通用响应格式

所有 REST 端点返回 JSON，Content-Type 为 `application/json`（导出端点除外）。

### 认证

当前版本无认证机制，通过 CORS 策略控制来源。

### 交互式文档

FastAPI 自动生成的 Swagger UI:
- **Swagger**: `http://127.0.0.1:8800/docs`
- **ReDoc**: `http://127.0.0.1:8800/redoc`

---

## 3. 健康检查与监控

### 3.1 GET `/api/health` — 进程存活检查

轻量级端点，确认服务进程运行中并返回 ASR / LLM 状态。

**请求**: 无参数

**响应** `200 OK`:

```json
{
  "status": "ok",
  "app": "meeting-realtime-voice",
  "python_version": "3.11.9",
  "runtime_warnings": [],
  "runtime": {
    "llm_config": { "provider": "ollama", "model": "qwen3.5:9b" },
    "asr_config": { "backend": "mlx", "device": "cpu", "model": "..." },
    "server_config": { "host": "0.0.0.0", "port": 8800 },
    "capabilities": { "mlx_available": true },
    "warnings": []
  },
  "asr_ready": true,
  "asr_state": "ready",
  "asr_error": null,
  "asr_model": "~/whisper-models/Qwen3-ASR-1.7B",
  "asr_device": "cpu",
  "llm_provider": "ollama",
  "llm_model": "qwen3.5:9b",
  "active_sessions": 0,
  "audio_queue_maxsize": 32
}
```

### 3.2 GET `/api/readiness` — 依赖就绪检查

检查所有依赖（ASR 模型、LLM）是否可用。适合 k8s readinessProbe。

**响应** `200 OK`:

```json
{
  "status": "ok",
  "runtime": { "..." },
  "asr": {
    "state": "ready",
    "error": null
  },
  "llm_status": "ollama: qwen3.5:9b @ http://localhost:11434",
  "active_sessions": 1
}
```

| `status` 值 | 含义 |
|-------------|------|
| `ok` | 所有依赖正常 |
| `degraded` | 部分依赖不可用 (如 ASR 未就绪) |

### 3.3 GET `/api/pipeline` — Pipeline 深度调试

返回每个活跃会话的音频队列深度和转写器状态。

**响应** `200 OK`:

```json
{
  "sessions": [
    {
      "session_id": "a1b2c3d4",
      "audio_queue_depth": 3,
      "audio_queue_maxsize": 32
    }
  ]
}
```

---

## 4. 文件上传转写

### POST `/api/upload` — 上传音频文件转写

上传音频文件，服务器执行 VAD 分段 + ASR 批量推理，返回完整转写结果。

**请求**: `multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | File | 是 | 音频文件 |
| `language` | string | 否 | 语言代码 (如 `zh`, `en`, `auto`)，默认自动检测 |
| `asr_prompt` | string | 否 | ASR 上下文提示 |

**支持的音频格式**:

| 格式 | MIME Type | 扩展名 |
|------|-----------|--------|
| WAV | `audio/wav`, `audio/x-wav` | `.wav` |
| MP3 | `audio/mp3`, `audio/mpeg` | `.mp3` |
| M4A | `audio/m4a`, `audio/x-m4a` | `.m4a` |
| OGG | `audio/ogg` | `.ogg` |
| FLAC | `audio/flac`, `audio/x-flac` | `.flac` |

**限制**: 最大 **500 MB**

**处理逻辑**:
- 音频 < 4s (预估): 单次直接推理
- 音频 >= 4s: 能量最小值分段 → MLX 批量 ASR
- 转写段落 >= 3 时自动异步生成摘要

**cURL 示例**:

```bash
curl -X POST http://127.0.0.1:8800/api/upload \
  -F "file=@meeting_recording.m4a" \
  -F "language=auto"
```

**响应** `200 OK`:

```json
{
  "session_id": "x7k9m2p4",
  "filename": "meeting_recording.m4a",
  "segments": [
    {
      "id": "seg-uuid-1",
      "speaker": "Speaker 1",
      "text": "大家好，今天的会议主要讨论...",
      "start_time": 0.0,
      "end_time": 120.5,
      "capture_start_time": 0.0,
      "capture_duration": 120.5,
      "timestamp": "2026-04-22T10:30:00"
    }
  ],
  "full_text": "大家好，今天的会议主要讨论...",
  "audio_duration": 3532.1,
  "processing_time": 132.2,
  "summary_state": "queued",
  "summary_message": "Summary generation queued (30 segments)"
}
```

**错误响应**:

| 状态码 | 场景 | 响应体 |
|--------|------|--------|
| `400` | 不支持的格式 | `{"detail": "Unsupported file type: video/mp4. Supported: WAV, MP3, M4A, OGG, FLAC"}` |
| `400` | 文件过大 | `{"detail": "File too large. Maximum size is 500MB."}` |
| `400` | 空文件 | `{"detail": "Empty file."}` |
| `500` | 转写失败 | `{"detail": "Transcription failed: <error>"}` |

> **前端注意**: 长音频转写可能耗时数分钟，axios 等 HTTP 客户端需设置 `timeout: 0` 禁用超时。

---

## 5. 会议历史管理

### 5.1 GET `/api/meetings` — 会议列表

获取所有持久化的会议记录。

**响应** `200 OK`:

```json
{
  "meetings": [
    {
      "session_id": "a1b2c3d4",
      "created_at": "2026-04-22T10:30:00",
      "ended_at": "2026-04-22T11:30:00",
      "status": "completed",
      "transcript_count": 30,
      "chat_count": 5,
      "summary": "会议讨论了三个主要议题...",
      "recording_path": "recordings/a1b2c3d4.wav",
      "recording_bytes": 112640000,
      "source": "upload"
    }
  ]
}
```

| `source` 值 | 说明 |
|-------------|------|
| `live` | 实时 WebSocket 转写 |
| `upload` | 文件上传转写 |

| `status` 值 | 说明 |
|-------------|------|
| `completed` | 正常结束 |
| `disconnected` | 连接中断 |

### 5.2 GET `/api/meetings/{session_id}` — 会议详情

获取单个会议的完整数据，包含所有转写段落、聊天历史和摘要。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 8 字符会话 ID |

**响应** `200 OK`:

```json
{
  "session_id": "a1b2c3d4",
  "created_at": "2026-04-22T10:30:00",
  "ended_at": "2026-04-22T11:30:00",
  "status": "completed",
  "transcript_count": 30,
  "chat_count": 5,
  "summary": "会议讨论了三个主要议题...",
  "summary_updated_at": "2026-04-22T11:31:00",
  "summary_turn_count": 30,
  "language": "zh",
  "transcript": [
    {
      "id": "seg-uuid-1",
      "speaker": "Speaker 1",
      "text": "大家好，今天的会议主要讨论...",
      "start_time": 0.0,
      "end_time": 120.5,
      "start": 0.0,
      "end": 120.5,
      "capture_start_time": 0.0,
      "capture_duration": 120.5
    }
  ],
  "chat_history": [
    {
      "id": "msg-uuid-1",
      "role": "user",
      "content": "会议的主要结论是什么?"
    },
    {
      "id": "msg-uuid-2",
      "role": "assistant",
      "content": "根据会议记录，主要结论包括..."
    }
  ],
  "recording_path": "recordings/a1b2c3d4.wav",
  "recording_bytes": 112640000,
  "recording_error": null,
  "source": "upload"
}
```

**错误**: `404` — `{"detail": "Meeting not found"}`

### 5.3 DELETE `/api/meetings/{session_id}` — 删除会议

删除会议及其关联的录音文件。

**路径参数**: `session_id` (string)

**响应** `200 OK`:

```json
{
  "status": "deleted",
  "session_id": "a1b2c3d4"
}
```

**错误**: `404` — `{"detail": "Meeting not found"}`

---

## 6. 会议导出

三种格式的导出端点，返回文件下载响应。

### 6.1 GET `/api/meetings/{session_id}/export.txt` — 纯文本导出

**Content-Type**: `text/plain; charset=utf-8`
**Content-Disposition**: `attachment; filename="meeting-{session_id}.txt"`

**响应体格式**:

```
Speaker 1 [0:00-2:00]: 大家好，今天的会议主要讨论...
Speaker 1 [2:01-4:30]: 第一个议题是关于...
```

### 6.2 GET `/api/meetings/{session_id}/export.md` — Markdown 导出

**Content-Type**: `text/markdown; charset=utf-8`
**Content-Disposition**: `attachment; filename="meeting-{session_id}.md"`

**响应体格式**:

```markdown
# Meeting a1b2c3d4

## Info
- Created: 2026-04-22T10:30:00
- Ended: 2026-04-22T11:30:00
- Status: completed

## Summary
会议讨论了三个主要议题...

## Transcript
**Speaker 1** [0:00-2:00]: 大家好，今天的会议主要讨论...

## Chat History
**User**: 会议的主要结论是什么?
**Assistant**: 根据会议记录...
```

### 6.3 GET `/api/meetings/{session_id}/export.json` — JSON 导出

**Content-Type**: `application/json`
**Content-Disposition**: `attachment; filename="meeting-{session_id}.json"`

响应体与 `GET /api/meetings/{session_id}` 相同。

---

## 7. 会议录音下载

### GET `/api/meetings/{session_id}/recording` — 下载录音

**Content-Type**: `audio/wav`

返回会议的 WAV 录音文件（实时会议自动录制、上传会议保留原始音频）。

**错误**:

| 状态码 | 场景 |
|--------|------|
| `404` | 会议未找到或无录音 (`"Recording not available"`) |
| `404` | 录音文件已被删除 (`"Recording file not found"`) |

---

## 8. 转写段落编辑

### PATCH `/api/meetings/{session_id}/transcript/{segment_id}` — 编辑转写段落

手动修正 ASR 转写错误。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会话 ID |
| `segment_id` | string | 段落 ID (8 字符) |

**请求体** (JSON):

```json
{
  "text": "修正后的转写文本",
  "speaker": "张三"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 | 修正后的文本 |
| `speaker` | string | 否 | 修正说话人名称 |

**响应** `200 OK`:

```json
{
  "segment": {
    "id": "seg-uuid-1",
    "speaker": "张三",
    "text": "修正后的转写文本",
    "start_time": 0.0,
    "end_time": 120.5,
    "capture_start_time": 0.0,
    "capture_duration": 120.5
  },
  "meeting": { "...完整会议对象..." }
}
```

**错误**:

| 状态码 | 场景 |
|--------|------|
| `400` | `text` 字段缺失 |
| `404` | 会议或段落未找到 |

---

## 9. 会议摘要

### POST `/api/meetings/{session_id}/summary` — 重新生成摘要

基于已有转写段落重新生成 LLM 摘要。使用 Ollama 本地 LLM (`qwen3.5:9b`)。

**请求体**: 无

**响应** `200 OK`:

```json
{
  "session_id": "a1b2c3d4",
  "summary": "本次会议讨论了三个主要议题: 1) ...",
  "summary_updated_at": "2026-04-22T11:31:00",
  "summary_turn_count": 30,
  "transcript_count": 30
}
```

**错误**:

| 状态码 | 场景 |
|--------|------|
| `400` | 转写段落不足 (`"Not enough transcript to generate summary"`) |
| `404` | 会议未找到 |

---

## 10. 会议聊天 (HTTP)

### POST `/api/meetings/{session_id}/chat` — AI 问答

基于会议上下文回答问题。LLM 会参考转写内容和摘要生成答案。

**请求体** (JSON):

```json
{
  "question": "会议的主要结论是什么?"
}
```

**响应** `200 OK`:

```json
{
  "answer": "根据会议记录，主要结论包括: 1) ..."
}
```

**错误**:

| 状态码 | 场景 |
|--------|------|
| `400` | `question` 缺失 |
| `404` | 会议未找到 |
| `500` | LLM 调用失败 |

---

## 11. 活跃会话查询

### GET `/api/sessions` — 活跃会话列表

> 需要在服务器配置中启用: `EXPOSE_SESSION_API=true`

**响应** `200 OK`:

```json
{
  "sessions": [
    {
      "session_id": "a1b2c3d4",
      "created_at": "2026-04-22T10:30:00",
      "transcript_count": 15,
      "chat_count": 2,
      "summary": null
    }
  ]
}
```

**错误**: `404` — 未启用时返回 `{"detail": "Not found"}`

---

## 12. WebSocket 实时转写

### WS `/ws/meeting` — 实时音频转写

双向 WebSocket 连接，支持实时音频流转写、AI 聊天和摘要生成。

### 12.1 连接

```javascript
const ws = new WebSocket("ws://127.0.0.1:8800/ws/meeting");
```

连接成功后，服务器立即发送 `ready` 消息:

```json
{
  "type": "ready",
  "session_id": "a1b2c3d4",
  "asr_device": "cpu",
  "asr_ready": true,
  "asr_init_timeout_sec": 240,
  "asr_model": "~/whisper-models/Qwen3-ASR-1.7B",
  "asr_language": "auto",
  "asr_state": "ready",
  "llm_provider": "ollama",
  "llm_model": "qwen3.5:9b",
  "runtime_warnings": [],
  "runtime": { "..." }
}
```

### 12.2 客户端 → 服务器消息

#### `start` — 开始录制转写

```json
{
  "type": "start",
  "language": "zh",
  "asr_prompt": "技术会议讨论"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `language` | string | 否 | 语言代码，默认自动检测 |
| `asr_prompt` | string | 否 | ASR 上下文提示 |

服务器响应流:

```json
{"type": "status", "message": "Preparing transcription model..."}
```
```json
{"type": "start_ack", "message": "Meeting started, transcribing..."}
```

#### `stop` / `eos` — 停止录制

```json
{
  "type": "stop",
  "last_seq": 12345
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `last_seq` | int | 否 | 最后的音频序列号 |

服务器响应:

```json
{
  "type": "stopped",
  "message": "Meeting ended",
  "last_seq": 12345,
  "reason": "stop",
  "recording_path": "recordings/a1b2c3d4.wav",
  "recording_bytes": 112640000,
  "recording_error": null
}
```

#### 音频数据 (Binary) — 发送音频帧

支持两种二进制格式:

**格式 A: 原始 PCM**

直接发送 16-bit LE PCM 音频字节。

**格式 B: MRV1 分包协议**

```
┌──────────────────────────────────────────┐
│ Magic: "MRV1"              (4 bytes)     │
│ Frame Count: uint16        (2 bytes)     │
├──────────────────────────────────────────┤
│ Frame 1:                                 │
│   Sequence: uint32         (4 bytes)     │
│   Sample Count: uint16     (2 bytes)     │
│   PCM Data: int16[]        (N×2 bytes)   │
├──────────────────────────────────────────┤
│ Frame 2: ...                             │
└──────────────────────────────────────────┘
```

**音频要求**: 16kHz 采样率，单声道，16-bit signed integer PCM

#### `chat` — AI 问答

```json
{
  "type": "chat",
  "question": "刚才讨论的要点是什么?"
}
```

服务器以流式方式回复:

```json
{"type": "chat_status", "status": "thinking", "message": "AI thinking..."}
```
```json
{"type": "chat_stream_start", "message_id": "msg-1", "question": "刚才讨论的要点是什么?"}
```
```json
{"type": "chat_stream_delta", "message_id": "msg-1", "delta": "根据"}
```
```json
{"type": "chat_stream_delta", "message_id": "msg-1", "delta": "会议记录"}
```
```json
{
  "type": "chat_response",
  "message_id": "msg-1",
  "question": "刚才讨论的要点是什么?",
  "answer": "根据会议记录，讨论的要点包括..."
}
```

#### `summary` — 请求摘要

```json
{
  "type": "summary"
}
```

服务器响应:

```json
{"type": "summary_status", "status": "updating", "message": "Updating summary..."}
```
```json
{
  "type": "summary_update",
  "summary": "本次会议讨论了...",
  "transcript_count": 15
}
```

#### `ping` — 心跳

```json
{"type": "ping"}
```

响应: `{"type": "pong"}`

### 12.3 服务器 → 客户端消息

#### `transcript` — 转写结果

```json
{
  "type": "transcript",
  "segment": {
    "id": "seg-uuid-1",
    "segment_id": 3,
    "revision": 1,
    "is_final": true,
    "cut_reason": "endpoint",
    "speaker": "Speaker 1",
    "text": "今天的议题包括三个方面",
    "start": 15.2,
    "end": 18.7,
    "capture_start_time": 1713770400.0,
    "capture_duration": 3.5
  },
  "total_segments": 3,
  "processing_time": 0.45
}
```

| 字段 | 说明 |
|------|------|
| `segment_id` | 段落序号 (递增) |
| `revision` | 修订版本 (同一段落可能多次更新) |
| `is_final` | `true` = 最终版本, `false` = 中间预览 |
| `cut_reason` | `"endpoint"` = VAD 端点切分, `"semantic"` = 语义边界切分 |
| `start` / `end` | 音频时间戳 (秒) |
| `capture_start_time` | 系统时间戳 (Unix epoch) |
| `capture_duration` | 音频片段时长 (秒) |

#### `transcribe_done` — 转写完成确认

```json
{
  "type": "transcribe_done",
  "processing_time": 0.45,
  "phase": "final",
  "segment_id": 3,
  "revision": 1,
  "is_final": true,
  "cut_reason": "endpoint"
}
```

#### `error` — 错误通知

```json
{
  "type": "error",
  "message": "ASR initialization timeout"
}
```

### 12.4 完整交互时序

```
客户端                                     服务器
  │                                          │
  │──── WebSocket 连接 ─────────────────────>│
  │<──── {"type":"ready", ...} ─────────────│
  │                                          │
  │──── {"type":"start"} ──────────────────>│
  │<──── {"type":"status", "Preparing..."} ──│
  │<──── {"type":"start_ack"} ──────────────│
  │                                          │
  │──── [Binary PCM audio frames] ─────────>│
  │──── [Binary PCM audio frames] ─────────>│
  │<──── {"type":"transcript", ...} ────────│  (is_final: false)
  │──── [Binary PCM audio frames] ─────────>│
  │<──── {"type":"transcript", ...} ────────│  (is_final: true)
  │<──── {"type":"transcribe_done"} ────────│
  │                                          │
  │──── {"type":"chat", "question":"..."} ─>│
  │<──── {"type":"chat_status"} ────────────│
  │<──── {"type":"chat_stream_start"} ──────│
  │<──── {"type":"chat_stream_delta"} ──────│  ×N
  │<──── {"type":"chat_response"} ──────────│
  │                                          │
  │──── {"type":"summary"} ────────────────>│
  │<──── {"type":"summary_status"} ─────────│
  │<──── {"type":"summary_update"} ─────────│
  │                                          │
  │──── {"type":"stop"} ──────────────────->│
  │<──── {"type":"stopped", ...} ───────────│
  │                                          │
  │──── WebSocket 断开 ────────────────────>│
```

### 12.5 JavaScript 客户端示例

```javascript
const ws = new WebSocket("ws://127.0.0.1:8800/ws/meeting");

ws.onopen = () => {
  console.log("Connected");
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "ready":
      // 连接就绪，发送 start
      ws.send(JSON.stringify({ type: "start", language: "auto" }));
      break;

    case "start_ack":
      // 开始发送音频
      startAudioCapture();
      break;

    case "transcript":
      const seg = msg.segment;
      if (seg.is_final) {
        appendFinalTranscript(seg.text);
      } else {
        updatePreviewTranscript(seg.text);
      }
      break;

    case "chat_stream_delta":
      appendChatDelta(msg.delta);
      break;

    case "chat_response":
      finalizeChatResponse(msg.answer);
      break;

    case "summary_update":
      updateSummaryPanel(msg.summary);
      break;

    case "stopped":
      console.log("Meeting ended", msg.recording_path);
      break;

    case "error":
      console.error("Server error:", msg.message);
      break;
  }
};

// 发送音频帧 (从 AudioWorklet 获取 PCM)
function sendAudioFrame(pcm16Buffer) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(pcm16Buffer);  // ArrayBuffer of int16 PCM
  }
}

// 停止会议
function stopMeeting() {
  ws.send(JSON.stringify({ type: "stop" }));
}

// 发送聊天问题
function askQuestion(question) {
  ws.send(JSON.stringify({ type: "chat", question }));
}

// 请求摘要
function requestSummary() {
  ws.send(JSON.stringify({ type: "summary" }));
}
```

---

## 13. 数据结构定义

### TranscriptSegment

```typescript
interface TranscriptSegment {
  id: string;               // UUID
  speaker: string;          // 说话人 (默认 "Speaker 1")
  text: string;             // 转写文本
  start_time: number;       // 音频起始时间 (秒)
  end_time: number;         // 音频结束时间 (秒)
  start: number;            // 同 start_time (WebSocket 兼容)
  end: number;              // 同 end_time (WebSocket 兼容)
  capture_start_time: number; // 系统时间戳 (Unix epoch)
  capture_duration: number;   // 音频片段时长 (秒)
  timestamp?: string;       // ISO 格式时间戳 (仅上传返回)
}
```

### ChatMessage

```typescript
interface ChatMessage {
  id: string;       // UUID
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
}
```

### MeetingSummary

```typescript
interface MeetingSummary {
  text: string;          // 摘要文本
  updated_at: string;    // ISO 时间戳
  turn_count: number;    // 生成摘要时的转写段落数
}
```

### Meeting (完整对象)

```typescript
interface Meeting {
  session_id: string;
  created_at: string;
  ended_at: string | null;
  status: "completed" | "disconnected";
  transcript_count: number;
  chat_count: number;
  summary: string | null;
  summary_updated_at?: string;
  summary_turn_count?: number;
  language?: string;
  transcript: TranscriptSegment[];
  chat_history: ChatMessage[];
  recording_path: string | null;
  recording_bytes: number;
  recording_error: string | null;
  source: "live" | "upload";
}
```

---

## 14. 错误处理

### HTTP 错误响应格式

所有错误返回 FastAPI 标准格式:

```json
{
  "detail": "错误描述信息"
}
```

### 常见错误码

| 状态码 | 场景 |
|--------|------|
| `400 Bad Request` | 请求参数缺失或无效 (文件格式不支持、文件过大、字段缺失) |
| `404 Not Found` | 会议/段落/录音不存在 |
| `500 Internal Server Error` | 服务器内部错误 (转写失败、LLM 调用失败) |

### WebSocket 错误处理

WebSocket 连接中的错误通过 `error` 类型消息推送:

```json
{
  "type": "error",
  "message": "ASR initialization failed: timeout"
}
```

连接异常断开时，服务器自动清理会话并保存录音。

---

## 15. CORS 与安全

### CORS 配置

| 参数 | 环境变量 | 默认值 |
|------|---------|--------|
| 允许来源 | `CORS_ORIGINS` | `http://127.0.0.1:8800, http://localhost:8800` |
| 允许凭证 | `CORS_ALLOW_CREDENTIALS` | `false` |

多个来源用逗号分隔: `CORS_ORIGINS=http://localhost:3000,http://localhost:8800`

### 环境变量配置

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `HOST` | 绑定地址 | `127.0.0.1` |
| `PORT` | 监听端口 | `8800` |
| `EXPOSE_SESSION_API` | 启用 `/api/sessions` | `false` |
| `AUDIO_QUEUE_MAXSIZE` | 音频队列上限 | `32` |
| `MEETING_RECORDINGS_DIR` | 录音存储目录 | `recordings/` |
| `MEETING_DB_PATH` | SQLite 数据库路径 | `data/meeting_realtime_voice.sqlite3` |
| `MEETING_HISTORY_LIMIT` | 会议列表最大返回数 | `100` |

### 数据持久化

- **数据库**: SQLite3，位于 `data/meeting_realtime_voice.sqlite3`
- **录音**: WAV 格式，位于 `recordings/` 目录
- **数据保留**: 无自动清理，需手动删除或调用 `DELETE /api/meetings/{session_id}`
