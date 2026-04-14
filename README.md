# Meeting Realtime Voice

本项目是一个本地优先的会议实时语音助手：浏览器采集麦克风音频，经 WebSocket 发送到 FastAPI 服务端，服务端完成实时转录、会议内问答、摘要生成，并在会后保存 WAV 录音。

## 当前定位

- 适合单机自托管和内部 beta。
- 默认只绑定 `127.0.0.1:8800`。
- 当前支持的发布运行时是 Python `3.11` 和 `3.12`。
- Python `3.13+` 尤其是 `MPS` 路径仍属于 best-effort，不建议作为发布环境。

## 功能

- 实时会议转录
- 会议内 AI 问答
- 自动和手动刷新会议摘要
- 会议结束后自动保存 WAV 录音
- SQLite 持久化会议历史
- 历史会议回看、导出和删除
- 历史转录片段在线编辑与摘要重算

## 快速开始

### 1. 创建环境

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少确认以下变量：

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b

ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-1.7B
ASR_ALIGNER_PATH=
ASR_DEVICE=auto

HOST=127.0.0.1
PORT=8800
```

如果使用 OpenAI 兼容接口：

```env
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

### 3. 启动

```bash
python server.py
```

浏览器访问：

```text
http://127.0.0.1:8800
```

## 开发命令

如果本机有 `make`：

```bash
make install
make test
make test-e2e
make run
```

等价命令：

```bash
venv/bin/python -m unittest discover -s tests
node --test tests/test_ui_formatters.mjs
node tests/test_phase1_e2e.mjs
venv/bin/python server.py
```

## 关键配置

### 运行与安全

```env
HOST=127.0.0.1
PORT=8800
CORS_ORIGINS=http://127.0.0.1:8800,http://localhost:8800
CORS_ALLOW_CREDENTIALS=false
EXPOSE_SESSION_API=false
AUDIO_QUEUE_MAXSIZE=32
MEETING_RECORDINGS_DIR=recordings
MEETING_DB_PATH=data/meeting_realtime_voice.sqlite3
MEETING_HISTORY_LIMIT=100
```

说明：

- `EXPOSE_SESSION_API=false` 时，`/api/sessions` 不对外暴露。
- `AUDIO_QUEUE_MAXSIZE` 用于限制实时音频处理积压。
- `MEETING_RECORDINGS_DIR` 控制会后录音保存目录。
- `MEETING_DB_PATH` 控制会议历史的 SQLite 数据库位置。
- `MEETING_HISTORY_LIMIT` 控制历史列表最大返回数量。

### ASR

```env
ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-1.7B
ASR_ALIGNER_PATH=
ASR_ALIGNER_BACKEND=qwen3alignment
ASR_LANGUAGE=
ASR_DEVICE=auto
ASR_ATTN_IMPLEMENTATION=auto
ASR_INIT_TIMEOUT_SEC=240
```

### Realtime Preview/Final Split

- realtime preview uses `PREVIEW_ASR_MODEL_PATH` and is intended for low-latency partial text
- finalized transcript segments use `ASR_MODEL_PATH`
- preview output is not persisted to meeting history
- finalized output is the only source for meeting summary and QA context

### Rollout Note

The realtime transcript UI is compatible with both:

- final-only realtime transcript events
- preview + final transcript events that share a stable `segment_id`

This allows a staged backend rollout without breaking the frontend.

## API

| Endpoint | Method | Description |
| --- | --- | --- |
| `/ws/meeting` | WebSocket | 主会话，承载转录、摘要、聊天 |
| `/api/health` | GET | 轻量 liveness 检查 |
| `/api/readiness` | GET | ASR/LLM 依赖状态检查 |
| `/api/meetings` | GET | 列出已保存会议历史 |
| `/api/meetings/{session_id}` | GET | 获取单场会议详情 |
| `/api/meetings/{session_id}/export.txt` | GET | 导出纯文本转录 |
| `/api/meetings/{session_id}/export.md` | GET | 导出 Markdown 会议记录 |
| `/api/meetings/{session_id}/export.json` | GET | 导出 JSON 会议明细 |
| `/api/meetings/{session_id}/recording` | GET | 下载会议录音 |
| `/api/meetings/{session_id}/transcript/{segment_id}` | PATCH | 编辑单条转录 |
| `/api/meetings/{session_id}/summary` | POST | 基于已保存转录重算摘要 |
| `/api/meetings/{session_id}` | DELETE | 删除会议及录音 |
| `/api/sessions` | GET | 仅在 `EXPOSE_SESSION_API=true` 时可用 |
| `/` | GET | 前端页面 |

## 运行说明

- 前端和后端默认同源工作，不需要额外跨域配置。
- 会议结束后，服务端会在 `recordings/` 下写入一份 WAV 文件。
- 会议元数据、转录、摘要和问答会持久化到 `MEETING_DB_PATH` 指定的 SQLite 文件。
- 前端右侧历史面板可回看会议、导出 TXT/Markdown/JSON、下载录音、删除会议，并支持直接修订转录内容。

## Docker 一键部署

使用 Docker Compose 一键启动所有服务（Ollama + 应用）：

```bash
# 1. 下载 ASR 模型（首次需要）
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B

# 可选：下载 preview 模型（低延迟实时预览）
huggingface-cli download Qwen/Qwen3-ASR-0.6B --local-dir ~/whisper-models/Qwen3-ASR-0.6B

# 2. 配置环境变量（可选，默认配置即可运行）
cp .env.example .env

# 3. 一键启动
docker compose up -d

# 查看日志
docker compose logs -f app

# 停止服务
docker compose down
```

或使用 Make：

```bash
make docker-up      # 构建 + 启动
make docker-logs    # 查看日志
make docker-down    # 停止
```

启动后访问 `http://127.0.0.1:8800`。

### Docker Compose 架构

| 服务 | 说明 |
|------|------|
| `ollama` | LLM 推理服务，首次启动自动拉取模型 |
| `ollama-init` | 一次性任务：拉取配置的 LLM 模型 |
| `app` | 主应用服务，自动等待 Ollama 就绪后启动 |

### 自定义配置

```bash
# 修改 ASR 模型路径（默认 ~/whisper-models）
ASR_MODELS_HOST_PATH=/data/models docker compose up -d

# 修改端口
PORT=9000 docker compose up -d

# 修改 LLM 模型
OLLAMA_MODEL=qwen3:14b docker compose up -d

# 使用 OpenAI API（跳过 Ollama）
# 在 .env 中设置 OPENAI_API_KEY 和 OPENAI_BASE_URL
docker compose up -d app  # 只启动 app，不启动 Ollama
```

### GPU 加速

Docker 中使用 GPU 需要安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)，然后取消 `docker-compose.yml` 中 `ollama` 服务的 GPU 配置注释。

## 故障排除

### ASR 模型加载失败

- 先确认 `ASR_MODEL_PATH` 指向已下载模型。
- 如果是 Apple Silicon，优先在 Python `3.11/3.12` 下测试。
- 如果 `MPS` 启动不稳定，先切到 `ASR_DEVICE=cpu` 验证链路。

### LLM 无法连接

```bash
curl http://localhost:11434/api/tags
```

或检查你的 OpenAI 兼容接口是否可访问。

## 测试状态

当前基线包含：

- Python 单测覆盖 ASR、WebSocket、持久化和 HTTP 历史接口
- UI formatter 有独立 Node 测试
- Phase 1 历史面板有独立 headless Chrome 端到端测试
- 增加了基础发布骨架：`pyproject.toml`、`Dockerfile`、`Makefile`、CI

## License

MIT
