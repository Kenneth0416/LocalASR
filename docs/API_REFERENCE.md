# Meeting Realtime Voice — API Reference

> 本文档是 **会议实时语音助手** 的完整 API 参考手册，涵盖所有 REST 接口和 WebSocket 协议。
>
> **版本：** 1.0（未版本化）
> **Base URL（默认）：** `http://127.0.0.1:8800`
> **Swagger 文档：** `/docs`
> **前缀：** `/api`

---

## 目录

- [概述](#概述)
- [基础信息](#基础信息)
- [REST API](#rest-api)
  - [健康检查](#健康检查)
  - [会议管理](#会议管理)
  - [文件上传转录](#文件上传转录)
- [WebSocket API](#websocket-api)
  - [连接](#连接)
  - [消息协议](#消息协议)
  - [二进制音频协议](#二进制音频协议)
  - [完整会话流程](#完整会话流程)
- [数据模型](#数据模型)
- [错误处理](#错误处理)
- [配置参考](#配置参考)

---

## 概述

会议实时语音助手提供两套并行的 API 接口：

| 接口类型 | 用途 | 数据格式 |
|---------|------|---------|
| **REST API** | 会议历史管理、文件转录、摘要生成 | JSON / Multipart |
| **WebSocket API** | 实时会议转录、聊天问答 | JSON 控制消息 + 二进制音频 |

系统支持两种 LLM Provider：

| Provider | 模型配置 | 说明 |
|----------|---------|------|
| `ollama`（默认） | `OLLAMA_BASE_URL` + `OLLAMA_MODEL` | 本地部署，推荐 qwen3.5:9b |
| `openai` | `OPENAI_BASE_URL` + `OPENAI_MODEL` | OpenAI 或兼容 API |

---

## 基础信息

### 请求与响应约定

- 所有 REST API 请求和响应均为 **JSON** 格式（除文件下载外）。
- `Content-Type: application/json`（除文件上传使用 `multipart/form-data`）。
- 所有时间戳格式为 **ISO 8601** 字符串：`"2026-04-16T10:30:00.000000"`。
- 音频时长单位为 **秒（float）**，精确到毫秒。

### 跨域（CORS）

默认允许以下来源访问：

```
http://127.0.0.1:8800
http://localhost:8800
```

可通过环境变量 `CORS_ORIGINS` 自定义，支持多个逗号分隔的域名。

---

## REST API

### 健康检查

#### GET `/api/health`

进程级存活探针。用于判断服务进程是否存活。

**请求**

```
GET /api/health
```

**响应 `200 OK`**

```json
{
  "status": "ok",
  "app": "meeting-realtime-voice",
  "python_version": "3.12.0",
  "runtime_warnings": [],
  "runtime": {
    "asr_loaded": true,
    "asr_init_ms": 2340,
    "llm_provider": "ollama",
    "llm_model": "qwen3.5:9b",
    "llm_reachable": true,
    "disk_free_gb": 120.5,
    "memory_used_gb": 4.2,
    "memory_total_gb": 16.0
  },
  "asr_ready": true,
  "asr_state": "ready",
  "asr_error": null,
  "asr_model": "/Users/xxx/whisper-models/Qwen3-ASR-1.7B",
  "asr_device": "mps",
  "llm_provider": "ollama",
  "llm_model": "qwen3.5:9b",
  "active_sessions": 1,
  "audio_queue_maxsize": 32
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `"ok"` 始终返回 |
| `asr_ready` | boolean | ASR 模型是否已就绪 |
| `asr_state` | string | ASR 状态：`ready` / `loading` / `error` |
| `active_sessions` | integer | 当前活跃的 WebSocket 会话数 |
| `runtime` | object | 运行时快照（含 ASR/LLM/系统状态） |

---

#### GET `/api/readiness`

依赖就绪探针。用于判断系统是否可接受新请求（ASR 模型加载完成 + LLM 可达）。

**请求**

```
GET /api/readiness
```

**响应 `200 OK`**

```json
{
  "status": "ok",
  "runtime": { ... },
  "asr": {
    "state": "ready",
    "error": null,
    "device": "mps",
    "model_path": "/Users/xxx/..."
  },
  "llm_status": {
    "reachable": true,
    "provider": "ollama",
    "model": "qwen3.5:9b",
    "latency_ms": 450
  },
  "active_sessions": 1
}
```

| `status` 值 | 含义 |
|------------|------|
| `ok` | ASR 就绪 + LLM 可达 |
| `degraded` | ASR 加载中或 LLM 不可达，仍可服务但功能受限 |

---

### 会议管理

#### GET `/api/meetings`

获取会议历史列表。

**请求**

```
GET /api/meetings
```

**响应 `200 OK`**

```json
{
  "meetings": [
    {
      "session_id": "a1b2c3d4",
      "created_at": "2026-04-16T10:30:00.000000",
      "updated_at": "2026-04-16T11:45:00.000000",
      "ended_at": "2026-04-16T11:44:00.000000",
      "status": "completed",
      "transcript_count": 47,
      "chat_count": 3,
      "summary": "本次会议讨论了产品迭代计划...",
      "summary_preview": "本次会议讨论了产品迭代计划...",
      "summary_updated_at": "2026-04-16T11:44:00.000000",
      "summary_turn_count": 47,
      "recording_path": "recordings/20260416-103000-a1b2c3d4.wav",
      "recording_bytes": 8421952,
      "recording_error": null
    }
  ]
}
```

> **注意：** 暂不支持分页和过滤，默认返回最近 100 条（可配置 `MEETING_HISTORY_LIMIT`）。

---

#### GET `/api/meetings/{session_id}`

获取单场会议的完整详情，包含转录、问答和摘要。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会议会话 ID |

**请求**

```
GET /api/meetings/a1b2c3d4
```

**响应 `200 OK`**

```json
{
  "session_id": "a1b2c3d4",
  "created_at": "2026-04-16T10:30:00.000000",
  "updated_at": "2026-04-16T11:45:00.000000",
  "ended_at": "2026-04-16T11:44:00.000000",
  "status": "completed",
  "transcript_count": 47,
  "chat_count": 3,
  "summary": "本次会议讨论了产品迭代计划...",
  "summary_preview": "本次会议讨论了产品迭代计划...",
  "summary_updated_at": "2026-04-16T11:44:00.000000",
  "summary_turn_count": 47,
  "recording_path": "recordings/20260416-103000-a1b2c3d4.wav",
  "recording_bytes": 8421952,
  "recording_error": null,
  "transcript": [
    {
      "id": "f3e2d1c0",
      "ordinal": 1,
      "speaker": "发言人",
      "text": "好，我们开始今天的会议。",
      "start_time": 0.0,
      "end_time": 2.3,
      "timestamp": "2026-04-16T10:30:00.000000"
    }
  ],
  "chat_history": [
    {
      "id": "b4c5d6e7",
      "ordinal": 1,
      "role": "user",
      "content": "会议的主题是什么？",
      "timestamp": "2026-04-16T10:35:00.000000"
    },
    {
      "id": "c6d7e8f9",
      "ordinal": 2,
      "role": "assistant",
      "content": "本次会议的主题是 Q2 产品迭代计划...",
      "timestamp": "2026-04-16T10:35:05.000000"
    }
  ]
}
```

**响应 `404 Not Found`**

```json
{ "detail": "Meeting not found" }
```

---

#### PATCH `/api/meetings/{session_id}/transcript/{segment_id}`

更新已保存的转录片段（编辑说话人或内容）。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会议会话 ID |
| `segment_id` | string | 转录片段 ID |

**请求体**

```json
{
  "text": "修正后的转录内容",
  "speaker": "张三"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 | 修正后的转录文本 |
| `speaker` | string | 否 | 修正后的说话人名称 |

**响应 `200 OK`**

```json
{
  "segment": {
    "id": "f3e2d1c0",
    "ordinal": 1,
    "speaker": "张三",
    "text": "修正后的转录内容",
    "start_time": 0.0,
    "end_time": 2.3,
    "timestamp": "2026-04-16T10:30:00.000000"
  },
  "meeting": { ...完整会议对象... }
}
```

**响应 `400 Bad Request`**

```json
{ "detail": "text is required" }
```

**响应 `404 Not Found`**

```json
{ "detail": "Transcript segment not found" }
```

---

#### POST `/api/meetings/{session_id}/summary`

重新生成会议摘要。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会议会话 ID |

**请求**

```
POST /api/meetings/a1b2c3d4/summary
```

**响应 `200 OK`**

```json
{
  "session_id": "a1b2c3d4",
  "summary": "## 会议摘要\n\n### 主题\nQ2 产品迭代计划评审...\n\n### 结论\n1. 同意 Q2 优先级...\n",
  "summary_updated_at": "2026-04-16T12:00:00.000000",
  "summary_turn_count": 47
}
```

---

#### DELETE `/api/meetings/{session_id}`

删除会议及所有关联数据。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会议会话 ID |

**请求**

```
DELETE /api/meetings/a1b2c3d4
```

**响应 `200 OK`**

```json
{ "status": "deleted", "session_id": "a1b2c3d4" }
```

**副作用：**
- 从 SQLite 数据库删除会议记录及所有转录、问答数据
- 删除 WAV 录音文件（如存在）

**响应 `404 Not Found`**

```json
{ "detail": "Meeting not found" }
```

---

#### GET `/api/meetings/{session_id}/export.txt`

以纯文本格式导出会话转录。

**请求**

```
GET /api/meetings/a1b2c3d4/export.txt
```

**响应 `200 OK`**

```
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="meeting-a1b2c3d4.txt"

[0.0s-2.3s] 发言人: 好，我们开始今天的会议。
[2.3s-5.8s] 发言人: 今天主要讨论 Q2 迭代计划。
[5.8s-8.1s] 发言人: 第一个议题是...
```

---

#### GET `/api/meetings/{session_id}/export.md`

以 Markdown 格式导出会话摘要（含元数据和转录）。

**请求**

```
GET /api/meetings/a1b2c3d4/export.md
```

**响应 `200 OK`**

````markdown
Content-Type: text/markdown; charset=utf-8
Content-Disposition: attachment; filename="meeting-a1b2c3d4.md"

# 会议记录

- **会议时间：** 2026-04-16 10:30
- **转录段数：** 47
- **状态：** 已完成

## 摘要

本次会议讨论了...

## 转录

[0.0s-2.3s] 发言人: 好，我们开始今天的会议。
...
````

---

#### GET `/api/meetings/{session_id}/export.json`

导出会议完整 JSON 数据。

**请求**

```
GET /api/meetings/a1b2c3d4/export.json
```

**响应 `200 OK`**

```
Content-Type: application/json
Content-Disposition: attachment; filename="meeting-a1b2c3d4.json"
```

返回内容同 `GET /api/meetings/{session_id}` 的完整响应体。

---

#### GET `/api/meetings/{session_id}/recording`

下载会议录音文件（WAV 格式）。

**请求**

```
GET /api/meetings/a1b2c3d4/recording
```

**响应 `200 OK`**

```
Content-Type: audio/wav
Content-Disposition: attachment; filename="20260416-103000-a1b2c3d4.wav"
<binary WAV data>
```

**响应 `404 Not Found`**

```json
{ "detail": "Recording not available" }
```

或

```json
{ "detail": "Recording file not found" }
```

---

#### POST `/api/meetings/{session_id}/chat`

向历史会议发送问答请求（HTTP 版，用于上传录音转录后的聊天）。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会议会话 ID |

**请求体**

```json
{
  "question": "会议中提到的产品发布日期是什么时候？"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 用户问题 |

**响应 `200 OK`**

```json
{
  "question": "会议中提到的产品发布日期是什么时候？",
  "answer": "根据会议记录，产品发布日期是 5 月 15 日...",
  "message_id": "x9y8z7w6"
}
```

> **注意：** 此接口仅推荐用于上传模式转录后的聊天。实时会议请使用 WebSocket API 中的 `chat` 消息。

---

### 文件上传转录

#### POST `/api/upload`

上传音频文件进行离线转录。

**请求**

```
POST /api/upload
Content-Type: multipart/form-data
```

| 表单字段 | 类型 | 必填 | 说明 |
|---------|------|------|------|
| `file` | File | 是 | 音频文件 |
| `language` | string | 否 | ASR 语言：`Chinese`、`English`、`Japanese`、`Korean`，空为自动检测 |
| `asr_prompt` | string | 否 | ASR 提示词，用于术语、人名、产品名约束 |

**支持的音频格式：**

| 格式 | MIME 类型 | 扩展名 |
|------|----------|--------|
| WAV | `audio/wav`, `audio/x-wav` | `.wav` |
| MP3 | `audio/mp3`, `audio/mpeg` | `.mp3` |
| M4A | `audio/m4a`, `audio/x-m4a` | `.m4a` |
| OGG | `audio/ogg` | `.ogg` |
| FLAC | `audio/flac`, `audio/x-flac` | `.flac` |

**文件限制：**
- 最大 500MB
- 非空文件

**响应 `200 OK`**

```json
{
  "session_id": "e5f6g7h8",
  "filename": "meeting-record-0416.mp3",
  "segments": [
    {
      "id": "a1b2c3d4",
      "speaker": "Speaker 1",
      "text": "好，我们开始今天的会议。",
      "start_time": 0.0,
      "end_time": 2.3,
      "timestamp": "2026-04-16T10:30:00.000000"
    }
  ],
  "full_text": "好，我们开始今天的会议。今天...",
  "audio_duration": 423.5,
  "processing_time": 38.2,
  "summary_state": "queued",
  "summary_message": "摘要已排队生成"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 新创建的会议会话 ID |
| `segments` | array | 转录片段列表 |
| `full_text` | string | 完整转录文本 |
| `audio_duration` | float | 音频时长（秒） |
| `processing_time` | float | 转录耗时（秒） |
| `summary_state` | string | `queued`（>=3 段）或 `unavailable`（<3 段） |

**响应 `400 Bad Request`**

```json
{ "detail": "Unsupported file type: video/mp4. Supported: WAV, MP3, M4A, OGG, FLAC" }
```

或

```json
{ "detail": "File too large. Maximum size is 500MB." }
```

**响应 `500 Internal Server Error`**

```json
{ "detail": "Transcription failed: ASR model inference error" }
```

---

## WebSocket API

### 连接

**URL：** `ws://127.0.0.1:8800/ws/meeting`

**协议：** WebSocket（支持 `ws` 和 `wss`，取决于服务启动配置）

**建立连接流程：**

```
Client                          Server
  |                                |
  |--- (TCP handshake) ---------->|
  |                                |
  |<-- WebSocket upgrade response --|
  |                                |
  |<-- {type: "ready", ...} -----|  ← 连接建立，立即收到服务端 ready 消息
  |                                |
  |--- {type: "start", ...} ---->|  ← 客户端发起开始转录
  |                                |
  |<-- {type: "start_ack"} -------|  ← 确认转录已开始
  |                                |
  |=== (binary PCM audio) =======>|  ← 实时音频流
  |                                |
  |<-- {type: "transcript"} -------|  ← 转录结果（按话语段）
  |                                |
  |<-- {type: "transcribe_done"} --|  ← 处理完成信号
  |                                |
  |--- {type: "chat", ...} ------>|  ← 发送聊天问题
  |<-- {type: "chat_stream_delta"} |  ← 流式回答（token 增量）
  |<-- {type: "chat_response"} ---|  ← 回答完成
  |                                |
  |--- {type: "stop"} ------------>|  ← 结束会议
  |<-- {type: "stopped"} ----------|  ← 最终状态
  |                                |
  |--- (WebSocket close) -------->|
```

---

### 消息协议

所有控制消息为 **JSON 格式**，在 WebSocket 的 `text` 帧中传输。

#### 客户端 → 服务端

##### `start` — 开始转录

```json
{
  "type": "start",
  "language": "Chinese",
  "asr_prompt": "这是一场产品评审会议，请保留产品名称原文。"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `language` | string | 否 | ASR 语言（空=自动检测） |
| `asr_prompt` | string | 否 | ASR 提示词 |

---

##### `stop` / `eos` — 结束转录

```json
{ "type": "stop" }
```

或

```json
{ "type": "eos", "last_seq": 12345 }
```

| 消息类型 | 语义 |
|---------|------|
| `stop` | 用户主动结束 → 会议状态为 `completed` |
| `eos` | 连接断开触发的优雅关闭 → 会议状态为 `disconnected` |
| `last_seq` | 可选，最后一个已发送音频帧的序号 |

> 客户端应优先发送 `stop`，并在断开连接前发送 `eos` 以确保录音文件正确关闭。

---

##### `chat` — 发送聊天问题

```json
{
  "type": "chat",
  "question": "会议中提到的 Q2 计划是什么？"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 用户问题 |

> 聊天为非阻塞操作，不会阻塞音频处理流水线。多个 `chat` 消息可并发发送。

---

##### `summary` — 请求刷新摘要

```json
{ "type": "summary" }
```

> 摘要也可在每 30 个转录段（可配置 `SUMMARY_INTERVAL_TURNS`）时自动触发。

---

##### `ping` — 心跳

```json
{ "type": "ping" }
```

**服务端响应：**

```json
{ "type": "pong" }
```

---

#### 服务端 → 客户端

##### `ready` — 连接就绪

连接建立后立即发送，包含会话信息和系统状态。

```json
{
  "type": "ready",
  "session_id": "a1b2c3d4",
  "asr_device": "mps",
  "asr_ready": true,
  "asr_init_timeout_sec": 180,
  "asr_model": "/Users/xxx/whisper-models/Qwen3-ASR-1.7B",
  "asr_language": "",
  "asr_state": "ready",
  "llm_provider": "ollama",
  "llm_model": "qwen3.5:9b",
  "runtime_warnings": [],
  "runtime": { ... }
}
```

---

##### `start_ack` — 转录开始确认

```json
{
  "type": "start_ack",
  "message": "会议开始，正在转录..."
}
```

在 ASR 模型就绪后发送，表示可以开始发送音频数据。

---

##### `status` — 状态通知

```json
{ "type": "status", "message": "正在准备转录模型..." }
```

用于向用户展示中间状态。

---

##### `transcript` — 转录片段（实时）

每当一段话语（utterance）识别完成时推送。

```json
{
  "type": "transcript",
  "segment": {
    "id": "f3e2d1c0",
    "segment_id": 1,
    "revision": 0,
    "is_final": true,
    "cut_reason": "endpoint",
    "speaker": "发言人",
    "text": "好，我们开始今天的会议。",
    "start": 0.0,
    "end": 2.3
  },
  "total_segments": 1,
  "processing_time": 0.45
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `segment.id` | string | 片段唯一 ID（8 字符 UUID 前缀） |
| `segment.segment_id` | integer | 话语段序号 |
| `segment.revision` | integer | 修订版本（VAD 模式下暂为 0） |
| `segment.is_final` | boolean | 是否为最终确认版本 |
| `segment.cut_reason` | string | 切分原因：`endpoint`（静音切分）、`max_duration`（达到最大时长 18s） |
| `segment.speaker` | string | 说话人，默认"发言人" |
| `segment.text` | string | 转录文本 |
| `segment.start` | float | 开始时间（秒） |
| `segment.end` | float | 结束时间（秒） |
| `processing_time` | float | ASR 推理耗时（秒） |

> 注意：服务端发送的字段名为 `start` / `end`，数据库中存储为 `start_time` / `end_time`，前端有兼容处理。

---

##### `transcribe_done` — 转录完成信号

每个 `transcript` 消息后紧跟此消息，表示该片段的 ASR 处理流程已完成。

```json
{
  "type": "transcribe_done",
  "processing_time": 0.45,
  "phase": "final",
  "segment_id": 1,
  "revision": 0,
  "is_final": true,
  "cut_reason": "endpoint"
}
```

---

##### `chat_status` — AI 思考中

```json
{
  "type": "chat_status",
  "status": "thinking",
  "message": "AI 思考中..."
}
```

---

##### `chat_stream_start` — 回答开始

```json
{
  "type": "chat_stream_start",
  "message_id": "b4c5d6e7",
  "question": "Q2 计划是什么？"
}
```

---

##### `chat_stream_delta` — 回答内容（流式 token）

```json
{
  "type": "chat_stream_delta",
  "message_id": "b4c5d6e7",
  "delta": "根据"
}
```

```json
{
  "type": "chat_stream_delta",
  "message_id": "b4c5d6e7",
  "delta": "会议记录，Q2 的主要计划是..."
}
```

每个 `delta` 是一个 token 增量，客户端需将所有 `delta` 依次追加拼接。

---

##### `chat_response` — 回答完成

```json
{
  "type": "chat_response",
  "message_id": "b4c5d6e7",
  "question": "Q2 计划是什么？",
  "answer": "根据会议记录，Q2 的主要计划是..."
}
```

---

##### `chat_error` — 回答失败

```json
{
  "type": "chat_error",
  "message_id": "b4c5d6e7",
  "message": "AI 回答失败: LLM 服务不可达"
}
```

---

##### `summary_status` — 摘要更新中

```json
{
  "type": "summary_status",
  "status": "updating",
  "message": "正在更新摘要..."
}
```

---

##### `summary_update` — 摘要内容更新

```json
{
  "type": "summary_update",
  "summary": "## 会议摘要\n\n### 主题\n...",
  "transcript_count": 47
}
```

---

##### `error` — 错误通知

```json
{
  "type": "error",
  "message": "转录模型初始化失败: timed out after 180s"
}
```

---

##### `stopped` — 会议正式结束

用户发送 `stop` 后，服务端在录音文件关闭后发送此消息。

```json
{
  "type": "stopped",
  "message": "会议结束",
  "last_seq": 12345,
  "reason": "stop",
  "recording_path": "recordings/20260416-103000-a1b2c3d4.wav",
  "recording_bytes": 8421952,
  "recording_error": null
}
```

---

##### `closed` — 连接关闭

服务端因超时等原因主动关闭连接时发送。

```json
{ "type": "closed", "message": "会话已结束" }
```

---

### 二进制音频协议

#### MRV1 帧协议（推荐）

每个 WebSocket **二进制帧** 包含一个或多个音频帧，采用 MRV1（Meeting Realtime Voice v1）二进制格式：

```
┌────────┬─────────┬────────────┬──────────────────────────────────┐
│ Magic  │ #Frames │  Frame 1   │  Frame 2...                      │
│ 4B     │  2B LE  │  seq(4B)   │  ...                             │
│ MRV1   │ uint16  │ +nchan(2B) │                                  │
│        │         │ +PCM(n×2B)  │                                  │
└────────┴─────────┴────────────┴──────────────────────────────────┘
```

**帧结构：**

| 偏移 | 大小 | 类型 | 说明 |
|------|------|------|------|
| 0 | 4 | bytes | Magic: `0x4D 0x52 0x56 0x31` ("MRV1") |
| 4 | 2 | uint16 LE | 帧数量（1-255） |
| 6 | 4 | uint32 LE | 帧 1 序号（seq） |
| 10 | 2 | uint16 LE | 帧 1 样本数（sample_count） |
| 12 | N×2 | int16 LE | PCM 数据（N = sample_count） |
| ... | | | 后续帧继续 |

> **关键约束：**
> - **采样率：** 16kHz（`AUDIO_SAMPLE_RATE = 16000`）
> - **位深：** 16-bit signed, little-endian（PCM16LE）
> - **通道数：** 1（单声道）
> - **帧时长：** 默认 20ms，对应 `320` 样本
> - **批量发送：** 建议每 4 帧（80ms）打包为一个二进制帧发送

**JS 示例：**

```javascript
function buildAudioPacket(frames) {
  let totalBytes = 6; // header
  for (const frame of frames) {
    totalBytes += 6 + frame.buffer.byteLength;
  }

  const packet = new ArrayBuffer(totalBytes);
  const header = new Uint8Array(packet, 0, 4);
  header.set([0x4D, 0x52, 0x56, 0x31]); // "MRV1"

  const view = new DataView(packet);
  view.setUint16(4, frames.length, true); // little-endian

  let offset = 6;
  for (const frame of frames) {
    view.setUint32(offset, frame.seq >>> 0, true);
    offset += 4;
    view.setUint16(offset, frame.sampleCount, true);
    offset += 2;
    new Uint8Array(packet, offset, frame.buffer.byteLength).set(
      new Uint8Array(frame.buffer)
    );
    offset += frame.buffer.byteLength;
  }

  return packet;
}

// 发送（每80ms发送4帧）
ws.send(buildAudioPacket(pendingFrames));
```

#### 遗留兼容（Legacy Raw PCM）

为兼容旧版客户端，服务端也接受无帧头的原始 PCM 数据：

- 直接发送 16-bit PCM16LE 字节流
- 服务端自动按 `frame_samples = 320` 切分
- **不推荐新集成使用此模式**

---

### 完整会话流程

以下是一个完整的 WebSocket 会话时序：

```
1. Client  →  (TCP + WebSocket handshake)
2. Server ←  {type: "ready", session_id: "a1b2c3d4", ...}
3. Client  →  {type: "start", language: "Chinese", asr_prompt: "..."}
4. Server →  {type: "status", message: "正在准备转录模型..."}
             (ASR 模型初始化，等待完成)
5. Server ←  {type: "start_ack"}
6. Client  →  [binary: MRV1 audio frames]
7. Server ←  {type: "transcript", segment: {text: "好", start: 0.0, end: 0.5}}
8. Server ←  {type: "transcribe_done", processing_time: 0.23}
9. Client  →  [binary: MRV1 audio frames]
10. Server ←  {type: "transcript", segment: {...}}
             ... (重复 6-10，直至用户结束)
11. Client →  {type: "chat", question: "会议提到哪些结论？"}
12. Server ←  {type: "chat_status", status: "thinking"}
13. Server ←  {type: "chat_stream_start", message_id: "m1n2o3p4"}
14. Server ←  {type: "chat_stream_delta", message_id: "m1n2o3p4", delta: "根"}
15. Server ←  {type: "chat_stream_delta", message_id: "m1n2o3p4", delta: "据会"}
16. Server ←  {type: "chat_response", message_id: "m1n2o3p4", answer: "根据会议..."}
17. Client →  {type: "stop"}
18. Server →  {type: "stopped", recording_path: "...", recording_bytes: 8421952}
19. (WebSocket 连接关闭)
```

---

## 数据模型

### 会议（Meeting）

```typescript
interface Meeting {
  session_id: string;           // 8字符会话ID (UUID前缀)
  created_at: string;           // ISO 8601 创建时间
  updated_at: string;           // ISO 8601 更新时间
  ended_at: string | null;      // ISO 8601 结束时间
  status: MeetingStatus;        // 会议状态
  transcript_count: number;      // 转录片段总数
  chat_count: number;           // 问答消息总数
  summary: string | null;       // 会议摘要（Markdown）
  summary_preview: string;      // 摘要预览（前160字符）
  summary_updated_at: string | null; // 摘要更新时间
  summary_turn_count: number;   // 生成摘要时的转录段数
  recording_path: string | null;// WAV 录音文件路径
  recording_bytes: number;      // 录音文件大小（字节）
  recording_error: string | null; // 录音错误信息
}

type MeetingStatus = "active" | "completed" | "disconnected";
```

### 转录片段（TranscriptSegment）

```typescript
interface TranscriptSegment {
  id: string;              // 片段唯一ID
  ordinal: number;         // 在会议中的序号（从1开始）
  speaker: string;         // 说话人名称
  text: string;            // 转录文本
  start_time: number;      // 开始时间（秒），API JSON 中用 start
  end_time: number;        // 结束时间（秒），API JSON 中用 end
  timestamp: string;        // ISO 8601 片段创建时间
}
```

### 问答消息（ChatMessage）

```typescript
interface ChatMessage {
  id: string;              // 消息唯一ID
  ordinal: number;         // 在会议中的序号
  role: "user" | "assistant"; // 消息角色
  content: string;         // 消息内容
  timestamp: string;       // ISO 8601 创建时间
}
```

### ASR 结果（ASRResult）

```typescript
interface ASRResult {
  text: string;             // 完整转录文本
  segments: ASRSegment[];   // 带时间戳的片段列表
  audio_duration: number;   // 音频时长（秒）
  processing_time: number;  // 推理耗时（秒）
  has_timestamps: boolean;  // 是否包含时间戳
}

interface ASRSegment {
  text: string;
  start: number;           // 开始时间（秒）
  end: number;             // 结束时间（秒）
}
```

### 运行时快照（RuntimeSnapshot）

```typescript
interface RuntimeSnapshot {
  asr_loaded: boolean;
  asr_init_ms: number;
  llm_provider: string;
  llm_model: string;
  llm_reachable: boolean;
  disk_free_gb: number;
  memory_used_gb: number;
  memory_total_gb: number;
}
```

---

## 错误处理

### HTTP 错误

所有 REST API 错误均返回标准 FastAPI `HTTPException`，格式如下：

```json
{
  "detail": "错误描述信息"
}
```

| HTTP 状态码 | 含义 | 常见原因 |
|------------|------|---------|
| `400 Bad Request` | 请求格式错误 | 文件类型不支持、内容为空、text 字段缺失 |
| `404 Not Found` | 资源不存在 | session_id 错误、会议已删除 |
| `413 Payload Too Large` | 文件过大 | 超过 500MB 限制 |
| `422 Unprocessable Entity` | 请求体验证失败 | 表单字段缺失 |
| `500 Internal Server Error` | 服务端错误 | ASR 推理失败、LLM 不可用、数据库错误 |

### WebSocket 错误

WebSocket 中的错误通过 JSON 消息发送：

```json
{ "type": "error", "message": "错误描述" }
```

| 错误场景 | `message` 示例 |
|---------|--------------|
| ASR 模型初始化超时 | `"转录模型初始化超时（>180 秒），请检查本地 ASR 模型"` |
| 音频包格式错误 | `"音频包格式错误: audio packet header too short"` |
| ASR 推理失败 | `"转写失败: <具体错误>"` |
| LLM 服务不可用 | `"AI 回答失败: LLM 服务暂时不可用"` |

---

## 配置参考

### 环境变量速查表

#### 服务器

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 服务监听地址 |
| `PORT` | `8800` | 服务监听端口 |
| `CORS_ORIGINS` | `http://127.0.0.1:8800,http://localhost:8800` | 允许的 CORS 源 |
| `EXPOSE_SESSION_API` | `false` | 是否暴露 `/api/sessions` |
| `AUDIO_QUEUE_MAXSIZE` | `32` | 音频队列最大长度 |
| `MEETING_RECORDINGS_DIR` | `recordings/` | 录音文件存储目录 |
| `MEETING_DB_PATH` | `data/meeting_realtime_voice.sqlite3` | SQLite 数据库路径 |
| `MEETING_HISTORY_LIMIT` | `100` | 历史列表最大条数 |

#### ASR

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ASR_MODEL_PATH` | `~/whisper-models/Qwen3-ASR-1.7B` | ASR 模型路径 |
| `ASR_DEVICE` | `auto` | 推理设备：`auto`/`cuda`/`mps`/`cpu` |
| `ASR_LANGUAGE` | `""`（自动） | ASR 强制语言 |
| `ASR_INIT_TIMEOUT_SEC` | `240` | 模型初始化超时（秒） |
| `ASR_MAX_NEW_TOKENS` | `256` | 最大输出 token 数 |
| `ASR_MIN_CHUNK_SEC` | `4.0` | 最小音频片段（秒） |
| `ASR_PREFERRED_CHUNK_SEC` | `8.0` | 偏好音频片段（秒） |
| `ASR_MAX_CHUNK_SEC` | `12.0` | 最大音频片段（秒） |
| `ASR_OVERLAP_SEC` | `1.5` | 片段重叠时长（秒） |

#### VAD（语音活动检测）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WEBRTC_VAD_FRAME_MS` | `20` | VAD 帧长（ms），仅支持 10/20/30 |
| `WEBRTC_VAD_AGGRESSIVENESS` | `2` | VAD 激进程度 0-3 |
| `WEBRTC_VAD_ENTER_SPEECH_FRAMES` | `2` | 进入语音所需连续帧数 |
| `WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES` | `36` | 静音切分阈值帧数（36帧=720ms@20ms） |
| `WEBRTC_VAD_MAX_UTTERANCE_SEC` | `18.0` | 最大话语段时长（秒） |

#### LLM

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 服务地址 |
| `OLLAMA_MODEL` | `qwen3.5:9b` | Ollama 模型名称 |
| `OPENAI_BASE_URL` | `""` | OpenAI 兼容 API 地址 |
| `OPENAI_API_KEY` | `""` | OpenAI API Key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI 模型名称 |
| `LOCAL_ONLY_MODE` | `true` | 是否强制仅使用本地 LLM |

#### 会议

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CONTEXT_CHARS` | `50000` | LLM 上下文最大字符数 |
| `SUMMARY_INTERVAL_TURNS` | `30` | 自动摘要触发间隔（转录段数） |

---

## 集成示例

### Python（requests 库）

```python
import requests

BASE = "http://127.0.0.1:8800"

# 健康检查
health = requests.get(f"{BASE}/api/health").json()
print(f"ASR 状态: {health['asr_state']}, LLM: {health['llm_model']}")

# 列出历史会议
meetings = requests.get(f"{BASE}/api/meetings").json()
for m in meetings["meetings"]:
    print(f"  {m['session_id']} - {m['status']} - {m['transcript_count']}段")

# 上传音频文件转录
with open("meeting.wav", "rb") as f:
    resp = requests.post(
        f"{BASE}/api/upload",
        files={"file": ("meeting.wav", f, "audio/wav")},
        data={"language": "Chinese"}
    ).json()
print(f"转录完成: {len(resp['segments'])} 段, 耗时 {resp['processing_time']}s")

# 发送聊天
chat = requests.post(
    f"{BASE}/api/meetings/{resp['session_id']}/chat",
    json={"question": "会议的主题是什么？"}
).json()
print(f"AI 回答: {chat['answer']}")
```

### JavaScript（WebSocket 实时转录）

```javascript
const WS_URL = "ws://127.0.0.1:8800/ws/meeting";
const ws = new WebSocket(WS_URL);
ws.binaryType = "arraybuffer";

ws.addEventListener("open", () => {
  console.log("已连接，等待 ready...");
});

ws.addEventListener("message", async (event) => {
  if (event.data instanceof ArrayBuffer) {
    // 二进制音频帧（服务端不会主动发，此处为协议定义占位）
    return;
  }

  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "ready":
      console.log("Session:", msg.session_id);
      // 发送开始转录
      ws.send(JSON.stringify({
        type: "start",
        language: "Chinese",
        asr_prompt: "这是一场产品评审会议。"
      }));
      break;

    case "start_ack":
      console.log("转录已开始，发送音频...");
      // 通过 AudioWorklet 采集并发送 PCM
      startAudioCapture();
      break;

    case "transcript":
      console.log(`[${msg.segment.start}s] ${msg.segment.text}`);
      break;

    case "chat_response":
      console.log("AI:", msg.answer);
      break;

    case "stopped":
      console.log("会议结束，录音:", msg.recording_path);
      ws.close();
      break;

    case "error":
      console.error("错误:", msg.message);
      break;
  }
});

// 发送聊天
function askAI(question) {
  ws.send(JSON.stringify({ type: "chat", question }));
}

// 结束会议
function endMeeting() {
  ws.send(JSON.stringify({ type: "stop" }));
}
```

### cURL

```bash
# 健康检查
curl http://127.0.0.1:8800/api/health | jq

# 上传音频转录
curl -X POST http://127.0.0.1:8800/api/upload \
  -F "file=@meeting.wav" \
  -F "language=Chinese" | jq

# 获取会议详情
curl http://127.0.0.1:8800/api/meetings/a1b2c3d4 | jq

# 下载录音
curl -O http://127.0.0.1:8800/api/meetings/a1b2c3d4/recording

# 导出为 Markdown
curl http://127.0.0.1:8800/api/meetings/a1b2c3d4/export.md -o meeting.md

# 重新生成摘要
curl -X POST http://127.0.0.1:8800/api/meetings/a1b2c3d4/summary | jq

# 删除会议
curl -X DELETE http://127.0.0.1:8800/api/meetings/a1b2c3d4 | jq
```

---

## 变更日志

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-04-16 | 初始版本，覆盖所有 REST 和 WebSocket 接口 |
