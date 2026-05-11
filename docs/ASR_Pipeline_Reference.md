# MeetScribe ASR Pipeline & Model Parameter Reference

> 最后更新: 2026-04-22 | 模型: Qwen3-ASR-1.7B-4bit (MLX) | 平台: Apple Silicon

---

## 1. Pipeline 架构总览

```
                         ┌─────────────────────────────────────────────┐
                         │              音频输入                        │
                         │   WebSocket 实时流  /  HTTP 文件上传          │
                         └────────┬──────────────────────┬─────────────┘
                                  │                      │
                    ┌─────────────▼──────────┐  ┌────────▼──────────────┐
                    │    实时转写路径          │  │   上传转写路径          │
                    │  WebRTC VAD → ASR       │  │  VAD 分段 → 批量 ASR   │
                    └─────────────┬──────────┘  └────────┬──────────────┘
                                  │                      │
                         ┌────────▼──────────────────────▼──────────┐
                         │           ASR 推理引擎                    │
                         │          MLX (Apple Silicon)               │
                         └────────────────────┬─────────────────────┘
                                              │
                         ┌────────────────────▼─────────────────────┐
                         │           后处理                          │
                         │   strip_hallucination() → 去重 → 时间戳   │
                         └────────────────────┬─────────────────────┘
                                              │
                         ┌────────────────────▼─────────────────────┐
                         │           输出                            │
                         │   文本 + 时间戳段落 → 持久化 → LLM 摘要    │
                         └──────────────────────────────────────────┘
```

---

## 2. 模型配置

### 2.1 ASR 模型

| 参数 | 值 | 环境变量 |
|------|-----|---------|
| 主模型 | `~/whisper-models/Qwen3-ASR-1.7B` | `ASR_MODEL_PATH` / `FINAL_ASR_MODEL_PATH` |
| MLX 量化模型 | `mlx-community/Qwen3-ASR-1.7B-4bit` | `ASR_MLX_MODEL_PATH` |
| 后端 | `mlx` (唯一) | `ASR_BACKEND` |
| 采样率 | 16000 Hz | `ASR_SAMPLE_RATE` |
| 语言 | `auto` (自动检测) | `ASR_LANGUAGE` |

### 2.2 LLM 模型 (会议摘要)

| 参数 | 值 | 环境变量 |
|------|-----|---------|
| Provider | Ollama (本地) | - |
| 模型 | `qwen3.5:9b` | `OLLAMA_MODEL` |
| 端点 | `http://localhost:11434` | `OLLAMA_BASE_URL` |

---

## 3. MLX 推理参数 (主力引擎)

> MLX 后端在 Apple Silicon 上比 PyTorch MPS 快约 15-27x

| 参数 | 默认值 | 调优值 | 环境变量 | 说明 |
|------|--------|--------|---------|------|
| `mlx_temperature` | 0.0 | **0.0** | `ASR_MLX_TEMPERATURE` | 0=贪婪解码 (argmax)，>0 启用采样 |
| `mlx_repetition_penalty` | ~~0.0~~ | **1.3** | `ASR_MLX_REPETITION_PENALTY` | 对最近生成的 token 施加概率惩罚，抑制重复 |
| `mlx_repetition_context_size` | ~~100~~ | **50** | `ASR_MLX_REPETITION_CONTEXT_SIZE` | 惩罚窗口 (token 数)，越小惩罚越集中 |
| `mlx_max_new_tokens` | 2048 | **2048** | `ASR_MLX_MAX_NEW_TOKENS` | 每 chunk 独立 token 预算 |
| `prefill_step_size` | 2048 | 2048 | - (硬编码) | KV-cache 预填充步长 |

**调优说明**:
- `repetition_penalty=1.3` + `context_size=50` 是经过实测的最佳组合：消除了 58.8 分钟录音中 100+ 次短语循环和 500+ 次单字重复，且不影响正常语音识别质量
- `temperature` 保持 0.0 (贪婪)：配合 repetition_penalty 已足够，采样会引入随机性降低确定性
- 如果仍有幻觉残留，可尝试 `repetition_penalty=1.5` 或 `temperature=0.1`

---

## 4. 上传转写 — 分段策略

### 5.1 MLX 路径: 能量最小值分段

| 参数 | 值 | 环境变量 | 说明 |
|------|-----|---------|------|
| `upload_chunk_sec` | 120.0s | `ASR_UPLOAD_CHUNK_SEC` | 目标分段长度 (~2 分钟) |
| `search_expand_sec` | 15.0s | - | 在目标点 ±15s 内搜索最低能量点 |
| 能量窗口 | 50ms | - | 滑动窗口计算二次能量 |
| `upload_min_audio_sec` | 30.0s | `ASR_UPLOAD_MIN_AUDIO_SEC` | 低于此值走单次推理 |

**分段算法**:
1. 音频 ≤ 120s → 不分段，直接整体推理
2. 音频 > 120s → 按 120s 间隔定位目标分割点
3. 在目标点 ±15s 范围内，用 50ms 滑动窗口计算能量
4. 选择能量最低点切分 (静音/停顿处)
5. 每个 chunk 独立推理，独立 token 预算

---

## 5. 实时转写 — WebRTC VAD 参数

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|---------|------|
| `frame_ms` | 20 | `WEBRTC_VAD_FRAME_MS` | 帧长，可选 10/20/30ms |
| `vad_aggressiveness` | 2 | `WEBRTC_VAD_AGGRESSIVENESS` | 0-3，越高越激进过滤非语音 |
| `enter_speech_frames` | 2 | `WEBRTC_VAD_ENTER_SPEECH_FRAMES` | 连续多少帧语音才开始录制 |
| `endpoint_silence_frames` | 36 | `WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES` | 连续静音帧数触发端点 (36×20ms=720ms) |
| `max_utterance_sec` | 18.0 | `WEBRTC_VAD_MAX_UTTERANCE_SEC` | 单次语音最大时长，超限强制切分 |
| `pre_roll_sec` | 0.2 | `WEBRTC_VAD_PRE_ROLL_SEC` | 语音开始前保留的上下文 |
| `min_final_audio_sec` | 0.1 | `WEBRTC_VAD_MIN_FINAL_AUDIO_SEC` | 低于 100ms 的片段丢弃 |

**状态机流程**:
```
静默缓冲 → [连续2帧语音] → 录制中 → [连续36帧静音 or 18s上限] → 密封 → ASR推理 → 发射文本
         ↑                                                              ↓
         └──────────────────────── 重置 ←────────────────────────────────┘
```

---

## 7. 语义分段 (Semantic Chunking)

> 用于实时流式转写的精细文本边界检测

| 参数 | 值 | 环境变量 | 说明 |
|------|-----|---------|------|
| `min_chunk_sec` | 4.0 | `ASR_MIN_CHUNK_SEC` | 最小累积时长才开始找边界 |
| `preferred_chunk_sec` | 8.0 | `ASR_PREFERRED_CHUNK_SEC` | 理想分段长度 |
| `max_chunk_sec` | 12.0 | `ASR_MAX_CHUNK_SEC` | 硬上限 |
| `overlap_sec` | 1.5 | `ASR_OVERLAP_SEC` | 前后 chunk 重叠上下文 |
| `endpoint_silence_sec` | 0.45 | `ASR_ENDPOINT_SILENCE_SEC` | 尾部静音检测 |
| `semantic_pause_sec` | 0.45 | `ASR_SEMANTIC_PAUSE_SEC` | 语义停顿阈值 (句间) |
| `semantic_soft_pause_sec` | 0.6 | `ASR_SEMANTIC_SOFT_PAUSE_SEC` | 软边界阈值 (逗号等) |
| `semantic_force_commit_sec` | 18.0 | `ASR_SEMANTIC_FORCE_COMMIT_SEC` | 强制发射超时 |
| `dedupe_tail_chars` | 120 | - | 去重窗口 (最近 120 字符) |

**强边界标点**: `。！？!?…）)`
**软边界标点**: `,，、;；:：` + 停顿 ≥ 0.6s

---

## 8. Hallucination 过滤

### 8.1 模型层 (生成时)

| 措施 | 值 |
|------|-----|
| 贪婪解码 | `temperature=0.0` |
| 重复惩罚 | `repetition_penalty=1.3` |
| 惩罚窗口 | 50 tokens |

### 8.2 后处理层 (`text_utils.strip_hallucination`)

应用位置: `mlx_transcriber.py` (每 chunk)

| 规则 | 检测条件 | 处理方式 |
|------|---------|---------|
| 单字重复 | 同一字符连续 ≥10 次 | 保留 2 个 |
| 短语循环 | 4-40 字子串连续重复 ≥3 次 | 保留 1 次 |
| 尾部循环 | 4-60 字模式在末尾重复 ≥2 次 | 保留 1 次 |

**安全阈值**: 仅处理 ≥20 字符的文本，正常语音不受影响

---

## 9. 实测性能基准

### 9.1 上传转写 (MLX 1.7B-4bit, Apple Silicon)

| 测试文件 | 时长 | 处理时间 | 实时倍速 | 段落数 |
|---------|------|---------|---------|-------|
| `新錄音 2(1).m4a` | 3532.1s (58.8 min) | 132.2s | **26.7x** | 30 |

### 9.2 实时倍速 (RTF) 参考

| 后端 | 模型 | RTF | 说明 |
|------|------|-----|------|
| MLX 1.7B-4bit | Qwen3-ASR | ~0.037 (27x) | Apple Silicon 主力 |
| MLX 0.6B-4bit | Qwen3-ASR | ~0.026 (38x) | 质量较低但最快 |

> RTF = processing_time / audio_duration，越小越快

---

## 10. 支持的音频格式

| 格式 | MIME Type | 处理方式 |
|------|-----------|---------|
| WAV | audio/wav, audio/x-wav | soundfile 直读 |
| MP3 | audio/mp3, audio/mpeg | FFmpeg 转码 → 16kHz mono PCM |
| M4A | audio/m4a, audio/x-m4a | FFmpeg 转码 → 16kHz mono PCM |
| OGG | audio/ogg | soundfile / FFmpeg |
| FLAC | audio/flac, audio/x-flac | soundfile 直读 |

**限制**: 最大 500MB，16kHz 单声道 float32 归一化到 [-1.0, 1.0]

---

## 11. 当前 .env 配置

```bash
# ── LLM ──────────────────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b

# ── ASR 核心 ─────────────────────────────────────
ASR_LANGUAGE=auto
ASR_BACKEND=mlx

# ── ASR 模型路径 ─────────────────────────────────
PREVIEW_ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-0.6B
PREVIEW_ASR_MAX_NEW_TOKENS=128
FINAL_ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-1.7B

# ── 服务器 ───────────────────────────────────────
HOST=0.0.0.0
PORT=8800
MAX_CONTEXT_MESSAGES=50
SUMMARY_INTERVAL_TURNS=30
```

---

## 12. 线程与并发模型

| 组件 | 类型 | 说明 |
|------|------|------|
| ASR Executor | `ThreadPoolExecutor(max_workers=2)` | 所有 ASR 推理共享 |
| asyncio 集成 | `loop.run_in_executor()` | 所有 CPU 密集任务不阻塞事件循环 |

---

## 13. 关键调优建议

### 速度优化
- 确保使用 `.venv/Python 3.11` (MLX 兼容)，而非 Python 3.14
- `ASR_BACKEND=mlx` 是唯一支持的后端
- `upload_chunk_sec=120` 对长音频已是最优平衡，降低到 30s 会增加 chunk 数但不显著提速

### 质量优化
- `mlx_repetition_penalty=1.3` 是安全值，1.5 更激进但可能影响正常重复短语
- `vad_aggressiveness=2` 适合大多数场景，嘈杂环境可提高到 3
- `max_utterance_sec=18` 避免单次语音过长导致模型注意力衰减

### 内存控制
- `mlx_max_new_tokens=2048` 对 120s chunk 绰绰有余 (CJK 密集语音约 500 字/2min)
