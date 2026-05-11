# Meeting Realtime Voice

**Local-first meeting transcription assistant — WebRTC VAD + Qwen3-ASR single-track realtime ASR**

[中文说明](#中文说明) | [English](#english)

---

## English

### Overview

Meeting Realtime Voice is a **local-first, privacy-preserving** meeting transcription system built with Python and FastAPI. It captures browser microphone audio via WebSocket, performs real-time speech-to-text using Qwen3-ASR driven by WebRTC VAD voice activity detection, and delivers live transcription, LLM-powered Q&A, and auto-generated summaries — all without sending data to the cloud.

**Core innovation**: A single-track ASR architecture where WebRTC VAD detects utterance endpoints (720ms silence), triggering a single Qwen3-ASR-1.7B inference pass per utterance. This achieves ~1.5s P99 latency with native support for 10+ Chinese dialects (Mandarin, Cantonese, Wu, Northeastern, Sichuanese, etc.) — a key differentiator from Whisper-based solutions.

### Architecture

```
Browser (getUserMedia)
    │ PCM audio
    ▼
FastAPI WebSocket (/ws/meeting)
    │
    ├── AudioDecoder ──▶ audio_queue (maxsize=32, backpressure)
    │                       │
    │                       ▼
    │              WebRTCVADMeetingTranscriber
    │              (20ms frames, 720ms endpoint detection)
    │                       │
    │                       ▼
    │              FinalOnlyASRRouter
    │              (Qwen3-ASR-1.7B via MLX or PyTorch)
    │                       │
    │                       ▼
    │              RealtimeTranscriptEvent ──▶ WebSocket push
    │                       │
    │                       ▼
    │              MeetingSession + SQLite persistence
    │
    ├── handle_chat() ──▶ LLM (Ollama / OpenAI)
    │
    └── handle_summary() ──▶ LLM auto-summary
```

### Key Models

| Component | Model | Notes |
|-----------|-------|-------|
| ASR | `Qwen/Qwen3-ASR-1.7B` | Single-track, Qwen3 series, 30-language support |
| ASR (MLX) | `mlx-community/Qwen3-ASR-1.7B-4bit` | Apple Silicon, ~1GB, ~30x faster than PyTorch |
| ASR (fast) | `mlx-community/Qwen3-ASR-0.6B-4bit` | Preview lane, ~0.4GB |
| VAD | WebRTC VAD (`webrtcvad-wheels`) | 20ms frames, 720ms endpoint threshold |
| LLM | `qwen3.5:9b` via Ollama | Local; alt: OpenAI API |

### Key Features

- **Real-time transcription**: WebRTC VAD-driven single-track ASR, ~1.5s P99 latency
- **Multi-dialect support**: Native support for 10+ Chinese dialects (Cantonese, Wu, Northeastern, Sichuanese, etc.)
- **LLM Q&A**: Ask questions about the meeting content during or after the session
- **Auto-summary**: Meeting summary auto-generated as the session progresses
- **Noise suppression**: AGC (Automatic Gain Control) + noise suppression preprocessing
- **Hallucination stripping**: Regex-based post-processing to remove ASR hallucinations
- **Privacy-first**: `local_only=True` by default; all inference runs locally
- **Apple Silicon**: MLX 4-bit quantization, ~30x faster than PyTorch MPS
- **Docker-ready**: One-command deployment with Ollama orchestration

### Quick Start

#### Python (Local)

```bash
# 1. Create environment (Python 3.11 or 3.12 recommended)
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 2. Download ASR model
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B

# 3. Configure environment
cp .env.example .env

# 4. Start Ollama (LLM backend)
ollama serve &
ollama pull qwen3.5:9b

# 5. Launch application
python server.py
# Server starts on http://127.0.0.1:8800
```

#### Docker (One-command)

```bash
# 1. Download ASR model
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B

# 2. Start all services (Ollama + App)
docker compose up -d

# View logs
docker compose logs -f app

# Stop
docker compose down
```

Visit `http://127.0.0.1:8800` after startup.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_MODEL` | `qwen3.5:9b` | LLM model via Ollama |
| `OPENAI_API_KEY` | — | OpenAI API key (alternative to Ollama) |
| `ASR_MODEL_PATH` | `~/whisper-models/Qwen3-ASR-1.7B` | Qwen3-ASR model path |
| `ASR_DEVICE` | `auto` | Device: `auto` / `cuda` / `mps` / `cpu` |
| `ASR_BACKEND` | `mlx` | Backend: `mlx` (Apple Silicon) / `pytorch` |
| `VAD_AGGRESSIVENESS` | `2` | WebRTC VAD aggressiveness (0–3) |
| `ENDPOINT_SILENCE_FRAMES` | `36` | Frames of silence to trigger endpoint (720ms) |
| `MAX_UTTERANCE_SEC` | `18.0` | Max utterance length before forced cut |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8800` | HTTP port |

### WebSocket Protocol

Connect to `ws://127.0.0.1:8800/ws/meeting`.

**Send:**

```json
{"type": "start", "language": "zh"}
{"type": "chat", "question": "刚才讨论了什么？"}
{"type": "summary"}
{"type": "stop"}
```

Binary PCM16 audio packets (16kHz, 16-bit mono, 640 bytes per 20ms frame).

**Receive:**

```json
{"type": "ready", "model": "Qwen3-ASR-1.7B"}
{"type": "final_segment", "text": "...", "segment_id": "..."}
{"type": "chat_token", "token": "逐字"}
{"type": "chat_response", "text": "完整回答"}
{"type": "summary_update", "summary": "..."}
{"type": "session_ended"}
```

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Frontend SPA |
| `/ws/meeting` | WebSocket | Main session: transcription, Q&A, summary |
| `/api/health` | GET | Liveness check |
| `/api/readiness` | GET | ASR + LLM readiness |
| `/api/meetings` | GET | List saved meetings |
| `/api/meetings/{session_id}` | GET | Get meeting details |
| `/api/meetings/{session_id}/export.txt` | GET | Export as plain text |
| `/api/meetings/{session_id}/export.md` | GET | Export as Markdown |
| `/api/meetings/{session_id}/export.json` | GET | Export as JSON |
| `/api/meetings/{session_id}/recording` | GET | Download WAV recording |
| `/api/meetings/{session_id}/transcript/{segment_id}` | PATCH | Edit transcript segment |
| `/api/meetings/{session_id}/summary` | POST | Regenerate summary |
| `/api/meetings/{session_id}` | DELETE | Delete meeting and recording |

### Frontend Pages

- **Dashboard**: Stats, quick actions, recent meetings
- **MeetingRoom**: Live transcription, chat, auto-summary (real-time)
- **Library**: Browse, export (TXT/MD/JSON), download recordings, delete
- **Upload**: Offline transcription via file upload (WAV/MP3/M4A)
- **Templates**: Reusable meeting templates with custom prompts

### Testing

```bash
# Unit tests
make test

# E2E server tests
make test-e2e

# ASR benchmark (RTF)
python tests/bench_mlx_asr.py recordings/20260409-094158-a10ebb41.wav

# Pipeline benchmark (10 categories)
python tests/bench_pipeline_performance.py

# Compare 0.6B vs 1.7B models
python tests/compare_mlx_models.py recordings/20260409-094158-a10ebb41.wav

# UX latency benchmark
python tests/bench_user_experience.py
```

### Project Structure

```
meeting_realtime_voice/
├── server.py                 # FastAPI + WebSocket entry point
├── ws_handler.py             # WebSocket message routing
├── vad.py                    # WebRTCVADMeetingTranscriber (VAD state machine)
├── asr.py                    # FinalOnlyASRRouter (single-track ASR)
├── asr_service.py            # ASR model loading and inference
├── mlx_transcriber.py        # MLX ASR implementation (Apple Silicon)
├── asr_utils.py              # Qwen3-ASR output parsing
├── text_utils.py             # Hallucination stripping post-processing
├── audio_preprocessor.py     # Audio normalization and preprocessing
├── noise_suppression.py      # Noise suppression module
├── agc.py                    # Automatic Gain Control
├── chunker.py                # Semantic chunking for long audio
├── session.py                # MeetingSession (LLM chat + summary)
├── persistence.py            # MeetingStore (SQLite)
├── config.py                 # All config dataclasses
├── frontend/                 # React + Vite SPA
│   └── src/
│       ├── pages/            # Dashboard, MeetingRoom, Library, Upload, Templates
│       └── components/        # ChatMessage, TranscriptSegment, MarkdownRenderer...
├── tests/                    # Benchmark and test suite
├── docs/                     # API reference, architecture docs, reports
├── Dockerfile                # CPU-only Docker image
└── docker-compose.yml        # 3-service orchestration (Ollama + App)
```

### Performance Highlights

| Metric | Value |
|--------|-------|
| P99 transcription latency | ~1,500ms (includes 720ms VAD endpoint) |
| MLX 1.7B-4bit RTF | ~0.03x (~30x faster than real-time) |
| MLX 0.6B-4bit RTF | ~0.026x (~38x faster than real-time) |
| PyTorch MPS 1.7B RTF | 0.81x (slower than real-time) |
| Memory (Qwen3-ASR 1.7B) | ~1.5GB peak |
| Concurrent sessions | 8–10 per machine |
| Python pipeline overhead | < 4.1ms per utterance |

### Troubleshooting

**ASR model fails to load:**
- Verify `ASR_MODEL_PATH` points to a downloaded model directory
- On Apple Silicon, use Python 3.11/3.12; MPS on Python 3.14 is best-effort
- Try `ASR_DEVICE=cpu` to isolate the issue

**LLM not connecting:**
```bash
curl http://localhost:11434/api/tags
```
Ensure Ollama is running or your OpenAI API key is set in `.env`.

**High latency:**
- Check `ASR_DEVICE=mlx` on Apple Silicon for ~30x speedup vs PyTorch MPS
- Verify `AUDIO_QUEUE_MAXSIZE=32` is not being exceeded

### Documentation

- `docs/API_REFERENCE.md` — Full API reference
- `docs/ASR_Pipeline_Reference.md` — ASR pipeline deep-dive
- `docs/Production_Readiness_Guide.md` — Deployment and monitoring guide
- `docs/Semantic_Chunking_Deep_Dive.md` — Semantic chunking algorithm
- `docs/reports/PERF_REPORT_*.md` — Pipeline performance reports
- `docs/reports/UX_PERF_REPORT_*.md` — UX latency benchmark reports

### License

MIT

---

## 中文说明

### 项目概述

Meeting Realtime Voice 是一个**本地优先、隐私保护**的会议转录系统，基于 Python 和 FastAPI 构建。系统通过 WebSocket 接收浏览器麦克风音频，使用 WebRTC VAD 语音活动检测驱动 Qwen3-ASR 进行实时语音识别，并将转录文本、LLM 问答和自动摘要通过同一 WebSocket 实时推送给客户端。所有处理均在本地完成，数据不上云。

**核心创新**：单轨 ASR 架构——WebRTC VAD 检测语音端点（720ms 沉默），触发 Qwen3-ASR-1.7B 单次推理。实现约 1.5 秒 P99 延迟，并原生支持普通话、粤语、吴语、东北话、四川话等 10+ 种中文方言——这是相比 Whisper 方案的关键差异化优势。

### 核心架构

```
浏览器麦克风（getUserMedia）
    │ PCM 音频流
    ▼
FastAPI WebSocket（/ws/meeting）
    │
    ├── AudioDecoder ──▶ audio_queue（maxsize=32，背压机制）
    │                       │
    │                       ▼
    │               WebRTCVADMeetingTranscriber
    │               （20ms/帧，720ms 端点检测）
    │                       │
    │                       ▼
    │               FinalOnlyASRRouter
    │               （Qwen3-ASR-1.7B，MLX 或 PyTorch）
    │                       │
    │                       ▼
    │               RealtimeTranscriptEvent ──▶ WebSocket 推送
    │                       │
    │                       ▼
    │               MeetingSession + SQLite 持久化
    │
    ├── handle_chat() ──▶ LLM（Ollama / OpenAI）
    │
    └── handle_summary() ──▶ LLM 自动摘要
```

### 核心技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| ASR | `Qwen/Qwen3-ASR-1.7B` | 单轨架构，支持 30 种语言 |
| ASR（MLX） | `mlx-community/Qwen3-ASR-1.7B-4bit` | Apple Silicon，约 1GB，约 30 倍快于 PyTorch |
| ASR（快速预览） | `mlx-community/Qwen3-ASR-0.6B-4bit` | 约 0.4GB |
| VAD | WebRTC VAD | 20ms 帧，720ms 端点阈值 |
| LLM | `qwen3.5:9b`（Ollama） | 本地运行；备用：OpenAI API |

### 主要功能

- **实时转录**：WebRTC VAD 驱动单轨 ASR，P99 延迟约 1.5 秒
- **多方言支持**：原生支持 10+ 种中文方言（粤语、吴语、东北话、四川话等）
- **LLM 问答**：会议中或结束后随时提问，基于已转录内容回答
- **自动摘要**：会议进行中自动增量更新摘要
- **噪声抑制**：AGC（自动增益控制）+ 噪声抑制预处理
- **幻觉清理**：正则表达式后处理，去除 ASR 幻觉文本
- **隐私优先**：代码默认 `local_only=True`，所有推理本地运行
- **Apple Silicon**：MLX 4-bit 量化，比 PyTorch MPS 快约 30 倍
- **Docker 一键部署**：Ollama 编排，开箱即用

### 快速开始

#### Python 本地运行

```bash
# 1. 创建虚拟环境（推荐 Python 3.11 或 3.12）
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 2. 下载 ASR 模型
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B

# 3. 配置环境变量
cp .env.example .env

# 4. 启动 Ollama（LLM 后端）
ollama serve &
ollama pull qwen3.5:9b

# 5. 启动应用
python server.py
# 服务器启动于 http://127.0.0.1:8800
```

#### Docker 一键启动

```bash
# 1. 下载 ASR 模型
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B

# 2. 一键启动所有服务（Ollama + App）
docker compose up -d

# 查看日志
docker compose logs -f app

# 停止服务
docker compose down
```

启动后访问 `http://127.0.0.1:8800`。

### 关键配置参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OLLAMA_MODEL` | `qwen3.5:9b` | Ollama LLM 模型 |
| `OPENAI_API_KEY` | — | OpenAI API 密钥（Ollama 备选） |
| `ASR_MODEL_PATH` | `~/whisper-models/Qwen3-ASR-1.7B` | Qwen3-ASR 模型路径 |
| `ASR_DEVICE` | `auto` | 设备：`auto` / `cuda` / `mps` / `cpu` |
| `ASR_BACKEND` | `mlx` | 后端：`mlx`（Apple Silicon）/ `pytorch` |
| `VAD_AGGRESSIVENESS` | `2` | WebRTC VAD 灵敏度（0–3） |
| `ENDPOINT_SILENCE_FRAMES` | `36` | 沉默帧数触发端点（720ms） |
| `MAX_UTTERANCE_SEC` | `18.0` | 单段最大说话时长 |
| `HOST` | `127.0.0.1` | 绑定地址 |
| `PORT` | `8800` | HTTP 端口 |

### WebSocket 协议

连接到 `ws://127.0.0.1:8800/ws/meeting`。

**发送：**

```json
{"type": "start", "language": "zh"}
{"type": "chat", "question": "刚才讨论了什么？"}
{"type": "summary"}
{"type": "stop"}
```

二进制 PCM16 音频包（16kHz，16 位单声道，每 20ms 帧 640 字节）。

**接收：**

```json
{"type": "ready", "model": "Qwen3-ASR-1.7B"}
{"type": "final_segment", "text": "...", "segment_id": "..."}
{"type": "chat_token", "token": "逐字"}
{"type": "chat_response", "text": "完整回答"}
{"type": "summary_update", "summary": "..."}
{"type": "session_ended"}
```

### REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 前端 SPA 页面 |
| `/ws/meeting` | WebSocket | 主会话：转录、问答、摘要 |
| `/api/health` | GET | 存活检查 |
| `/api/readiness` | GET | ASR + LLM 就绪状态 |
| `/api/meetings` | GET | 列出已保存会议 |
| `/api/meetings/{session_id}` | GET | 获取会议详情 |
| `/api/meetings/{session_id}/export.txt` | GET | 导出为纯文本 |
| `/api/meetings/{session_id}/export.md` | GET | 导出为 Markdown |
| `/api/meetings/{session_id}/export.json` | GET | 导出为 JSON |
| `/api/meetings/{session_id}/recording` | GET | 下载 WAV 录音 |
| `/api/meetings/{session_id}/transcript/{segment_id}` | PATCH | 编辑转录段落 |
| `/api/meetings/{session_id}/summary` | POST | 重新生成摘要 |
| `/api/meetings/{session_id}` | DELETE | 删除会议及录音 |

### 前端页面

- **Dashboard**：统计概览、快速操作、最近会议
- **MeetingRoom**：实时转录、聊天、自动摘要（实时更新）
- **Library**：历史会议浏览、导出（TXT/MD/JSON）、下载录音、删除
- **Upload**：离线转录，支持文件上传（WAV/MP3/M4A）
- **Templates**：可复用的会议模板，支持自定义提示词

### 测试命令

```bash
# 单元测试
make test

# E2E 服务端测试
make test-e2e

# ASR RTF 基准测试
python tests/bench_mlx_asr.py recordings/20260409-094158-a10ebb41.wav

# 管线性能基准（10 个类别）
python tests/bench_pipeline_performance.py

# 0.6B vs 1.7B 模型对比
python tests/compare_mlx_models.py recordings/20260409-094158-a10ebb41.wav

# 用户体验延迟基准
python tests/bench_user_experience.py
```

### 项目文件结构

```
meeting_realtime_voice/
├── server.py                 # FastAPI + WebSocket 入口
├── ws_handler.py             # WebSocket 消息路由
├── vad.py                    # WebRTCVADMeetingTranscriber（VAD 状态机）
├── asr.py                    # FinalOnlyASRRouter（单轨 ASR）
├── asr_service.py            # ASR 模型加载与推理
├── mlx_transcriber.py        # MLX ASR 实现（Apple Silicon）
├── asr_utils.py              # Qwen3-ASR 输出解析
├── text_utils.py             # 幻觉文本清理后处理
├── audio_preprocessor.py     # 音频归一化预处理
├── noise_suppression.py      # 噪声抑制模块
├── agc.py                    # 自动增益控制
├── chunker.py                # 长音频语义分块
├── session.py                # MeetingSession（LLM 问答 + 摘要）
├── persistence.py            # MeetingStore（SQLite 持久化）
├── config.py                 # 所有配置 dataclass
├── frontend/                 # React + Vite 单页应用
│   └── src/
│       ├── pages/            # Dashboard, MeetingRoom, Library, Upload, Templates
│       └── components/       # ChatMessage, TranscriptSegment, MarkdownRenderer...
├── tests/                    # 基准测试与测试套件
├── docs/                     # API 参考、架构文档、报告
├── Dockerfile                # CPU-only Docker 镜像
└── docker-compose.yml        # 3 服务编排（Ollama + App）
```

### 性能指标

| 指标 | 数值 |
|------|------|
| P99 转录延迟 | 约 1,500ms（含 720ms VAD 端点等待） |
| MLX 1.7B-4bit RTF | 约 0.03x（约 30 倍快于实时） |
| MLX 0.6B-4bit RTF | 约 0.026x（约 38 倍快于实时） |
| PyTorch MPS 1.7B RTF | 0.81x（慢于实时） |
| Qwen3-ASR 1.7B 内存峰值 | 约 1.5GB |
| 单机并发会议数 | 8–10 个 |
| Python 管线开销 | < 4.1ms/ utterance |

### 故障排除

**ASR 模型加载失败：**
- 确认 `ASR_MODEL_PATH` 指向已下载的模型目录
- Apple Silicon 建议使用 Python 3.11/3.12
- 尝试 `ASR_DEVICE=cpu` 隔离问题

**LLM 无法连接：**
```bash
curl http://localhost:11434/api/tags
```
确保 Ollama 正在运行，或已在 `.env` 中正确设置 OpenAI API 密钥。

**延迟过高：**
- Apple Silicon 上确保使用 `ASR_DEVICE=mlx`（比 PyTorch MPS 快约 30 倍）
- 检查 `AUDIO_QUEUE_MAXSIZE=32` 队列是否积压

### 文档

- `docs/API_REFERENCE.md` — 完整 API 参考
- `docs/ASR_Pipeline_Reference.md` — ASR 管线深度解析
- `docs/Production_Readiness_Guide.md` — 部署与监控指南
- `docs/Semantic_Chunking_Deep_Dive.md` — 语义分块算法
- `docs/reports/PERF_REPORT_*.md` — 管线性能报告
- `docs/reports/UX_PERF_REPORT_*.md` — 用户体验延迟基准报告

### License

MIT
