# MeetScribe 语义分段算法深度解析

> 最后更新: 2026-04-22 | 涉及文件: `chunker.py`, `vad.py`, `asr_types.py`, `asr_service.py`

---

## 目录

1. [系统中三种分段策略总览](#1-系统中三种分段策略总览)
2. [实时路径: WebRTC VAD 状态机](#2-实时路径-webrtc-vad-状态机)
3. [语义分段路径: TransformerAudioChunker](#3-语义分段路径-transformeraudiochunker)
4. [上传路径: 能量最小值分段 + 时间戳后分段](#4-上传路径-能量最小值分段--时间戳后分段)
5. [三种分段策略对比](#5-三种分段策略对比)
6. [当前系统的准确度瓶颈分析](#6-当前系统的准确度瓶颈分析)
7. [提高准确度的改进方案](#7-提高准确度的改进方案)

---

## 1. 系统中三种分段策略总览

MeetScribe 中存在三个不同层面的"分段"逻辑，各自解决不同场景的切分问题:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       音频分段策略矩阵                                    │
├──────────────┬────────────────────────────────┬────────────────────────┤
│ 策略          │ 适用场景                        │ 切分信号源              │
├──────────────┼────────────────────────────────┼────────────────────────┤
│ WebRTC VAD   │ 实时转写 (当前生产路径)          │ 声学能量 (逐帧 VAD)     │
│ 状态机        │                                │                        │
├──────────────┼────────────────────────────────┼────────────────────────┤
│ Transformer  │ 实时转写 (备选语义路径)          │ 声学能量 + ASR 时间戳   │
│ AudioChunker │                                │ + 标点/停顿语义        │
├──────────────┼────────────────────────────────┼────────────────────────┤
│ 能量最小值    │ 文件上传批量转写                 │ 滑动窗口二次能量        │
│ + 后分段      │                                │ + Forced Aligner 时间戳│
└──────────────┴────────────────────────────────┴────────────────────────┘
```

---

## 2. 实时路径: WebRTC VAD 状态机

> 文件: `vad.py` — `WebRTCVADMeetingTranscriber`
> 当前生产环境使用的实时转写方案

### 2.1 状态机模型

```
                    ┌───────────────────────────────────────────────┐
                    │                                               │
                    ▼                                               │
            ┌──────────────┐                                       │
            │   IDLE       │                                       │
            │  (静默等待)   │                                       │
            │              │                                       │
            │ _active=None │                                       │
            │ _speech_run  │                                       │
            │  = 0         │                                       │
            └──────┬───────┘                                       │
                   │                                               │
                   │ 连续 enter_speech_frames 帧                    │
                   │ 检测到语音 (is_speech=True)                     │
                   │                                               │
                   ▼                                               │
            ┌──────────────┐                                       │
            │  RECORDING   │                                       │
            │  (录制语音)   │                                       │
            │              │                                       │
            │ _active =    │                                       │
            │  UtteranceState                                      │
            │ pcm_buffer   │                                       │
            │  持续追加     │                                       │
            └──────┬───────┘                                       │
                   │                                               │
                   │ 触发条件 (二选一):                               │
                   │ ① 连续 endpoint_silence_frames 帧静音           │
                   │ ② 语音时长 >= max_utterance_sec                 │
                   │                                               │
                   ▼                                               │
            ┌──────────────┐                                       │
            │   SEALED     │                                       │
            │  (密封送检)   │                                       │
            │              │                                       │
            │ sealed=True  │      ASR 推理完成                      │
            │ snapshot=    │──────────────────────────────────────>│
            │  bytes(pcm)  │      发射 RealtimeTranscriptEvent     │
            └──────────────┘                                       │
                                                                   │
                                  重置状态 ◄─────────────────────────┘
```

### 2.2 逐帧处理算法 (`push_frame`)

```python
# 每帧处理伪代码 (frame = 20ms PCM)
def push_frame(pcm_chunk, sample_count):
    is_speech = webrtc_vad.is_speech(pcm_chunk, 16000)
    
    if is_speech:
        speech_run += 1
        silence_run = 0
        
        # 语音刚开始: 暂存到 speech_buffer (还不创建 utterance)
        if active is None and speech_run == 1:
            speech_start_sample = frame_start
            speech_buffer.clear()
            # 回溯 pre_roll (0.2s) 的最近帧，保留语音前上下文
            capture_start, pre_roll_pcm = consume_pre_roll()
            speech_buffer.extend(pre_roll_pcm)
        
        if active is None:
            speech_buffer.extend(pcm_chunk)
    else:
        speech_run = 0
        silence_run += 1
    
    # 连续 2 帧语音 → 确认语音开始，创建 UtteranceState
    if active is None and is_speech and speech_run >= enter_speech_frames:
        active = UtteranceState(
            segment_id=next_segment_id++,
            start_sample=speech_start_sample,
            pcm_buffer=speech_buffer,
        )
    
    # 已在录制中
    if active is not None:
        active.pcm_buffer.extend(pcm_chunk)  # 追加每一帧 (含静音)
        
        if is_speech:
            active.last_speech_sample = frame_end
        
        # 端点检测: 连续 36 帧静音 (720ms) 或 18s 上限
        if silence_run >= endpoint_silence_frames:   # 36 × 20ms = 720ms
            seal_active("endpoint")
        elif utterance_duration >= max_utterance_sec: # 18.0s
            seal_active("max_duration")
```

### 2.3 关键参数与影响

| 参数 | 默认值 | 物理含义 | 调大影响 | 调小影响 |
|------|--------|---------|---------|---------|
| `enter_speech_frames` | 2 | 确认语音开始所需连续语音帧 | 更抗干扰，但吞掉短语音开头 | 更灵敏，但容易误触发 |
| `endpoint_silence_frames` | 36 | 触发端点的连续静音帧数 | 容忍更长停顿 (不切) | 更快切分，但可能切碎句子 |
| `max_utterance_sec` | 18.0 | 单次语音最大时长 | 更长的完整句 | 避免注意力衰减 |
| `pre_roll_sec` | 0.2 | 语音前保留的上下文 | 捕获更完整的词首 | 减少无效静音 |
| `min_final_audio_sec` | 0.1 | 最短有效片段 | 过滤更多噪声 | 保留更短语音 |
| `vad_aggressiveness` | 2 | WebRTC VAD 激进度 (0-3) | 更多判为静音 | 更多判为语音 |

### 2.4 VAD 方法的根本局限

WebRTC VAD 只看 **声学能量**，完全不理解语言内容:

```
✗ "这个方案...嗯...我觉得可以"
              ^720ms^
              ↑ VAD 在此切断
  段落1: "这个方案"
  段落2: "嗯 我觉得可以"
  → 语义上是一句话，被 720ms 的思考停顿切碎

✗ "第一点是成本控制第二点是效率提升第三点是风险管理..."
  → 说话人一口气说 20s 没停顿，被 max_utterance_sec 强制切断
  → 可能切在词语中间
```

---

## 3. 语义分段路径: TransformerAudioChunker

> 文件: `chunker.py` — `TransformerAudioChunker` + `SemanticMeetingTranscriber`
> 系统内置的高级分段策略，利用 ASR 模型的时间戳输出实现语义级切分

### 3.1 核心理念

与 VAD 只看声学信号不同，语义分段器的核心创新是:

**先做 ASR 推理获得带时间戳的文本 → 再根据标点、停顿和语义信号决定在哪里切分**

```
音频流                        ┌─────────────────────────┐
──────────────────────────────│  音频缓冲区              │
                              │  (持续累积 PCM)          │
                              └──────────┬──────────────┘
                                         │
                              ┌──────────▼──────────────┐
                              │  _choose_boundary()     │
                              │  选择推理窗口边界         │
                              │  (能量最小值 / 静音 /     │
                              │   硬上限)                 │
                              └──────────┬──────────────┘
                                         │
                              ┌──────────▼──────────────┐
                              │  ASR 推理                │
                              │  返回: text + segments[] │
                              │  每个 segment 有          │
                              │  {start, end, text}      │
                              └──────────┬──────────────┘
                                         │
                                         │ has_timestamps?
                                         │
                        ┌────────────────┴────────────────┐
                        │ False                           │ True
                        │ (无时间戳)                       │ (有时间戳)
                        ▼                                 ▼
                ┌───────────────┐              ┌───────────────────────┐
                │ 非语义模式     │              │  _semantic_commit_    │
                │               │              │  from_result()        │
                │ 整段发射       │              │                       │
                │ 保留 overlap   │              │  扫描 segments 寻找   │
                │ 滑动窗口前进   │              │  语义切分点            │
                └───────────────┘              └───────────┬───────────┘
                                                           │
                                                           ▼
                                               ┌───────────────────────┐
                                               │  SemanticCommit       │
                                               │  .text: 提交的文本     │
                                               │  .committed_bytes:    │
                                               │    精确到字节的音频偏移 │
                                               │  .should_wait: 继续等? │
                                               └───────────────────────┘
```

### 3.2 阶段一: 音频边界选择 (`_choose_boundary`)

`_choose_boundary` 决定 "本轮推理应该覆盖多大范围的音频缓冲区"。

```python
def _choose_boundary(final: bool) -> Optional[int]:
    total_duration = len(buffer) / bytes_per_second
    
    # ── 最终 flush: 全部推理 ──────────────────
    if final:
        return len(buffer)
    
    # ── 未达最小门槛 (4s): 继续积累 ──────────
    if total_duration < min_chunk_sec:     # 4.0s
        return None
    
    # ── 语义模式 (已获得过时间戳) ─────────────
    if semantic_mode:
        if has_trailing_silence():          # 尾部 0.45s 静音
            return len(buffer)              # → 立即推理全部
        
        if total_duration >= max_chunk_sec: # 12s
            cap = semantic_force_commit_sec * bps  # 18s 硬上限
            return min(len(buffer), cap)
        
        return None                         # 继续等待
    
    # ── 非语义模式 ────────────────────────────
    if has_trailing_silence():
        # 在 [min_chunk..now] 范围的最后 1.25s 内找能量最低点
        boundary = find_low_energy_boundary(
            start = max(min_chunk_sec, total_duration - boundary_search_sec),
            end   = total_duration,
        )
        if boundary:
            return boundary
    
    if total_duration >= max_chunk_sec:     # 12s
        # 在 preferred_chunk_sec ± boundary_search_sec 范围找能量最低点
        boundary = find_low_energy_boundary(
            start = max(min_chunk_sec, preferred_chunk_sec - boundary_search_sec),
            end   = min(total_duration, preferred_chunk_sec + boundary_search_sec),
        )
        return boundary or preferred_chunk_sec  # 找不到就硬切
    
    return None                             # 继续等待
```

#### 能量最小值搜索 (`_find_low_energy_boundary`)

```python
def _find_low_energy_boundary(start_sec, end_sec) -> Optional[int]:
    audio = buffer_to_float32()
    left  = int(start_sec * sample_rate)
    right = int(end_sec * sample_rate)
    
    # 滑动窗口宽度: 120ms (energy_window_ms)
    win = int(0.12 * sample_rate)  # 1920 samples
    
    # 计算绝对值滑动求和 (NOT 二次能量，轻量级)
    seg_abs = np.abs(audio[left:right])
    window_sums = np.convolve(seg_abs, np.ones(win), mode="valid")
    
    # 全局最小位置
    min_pos = np.argmin(window_sums)
    
    # 在最小窗口内精确定位最低采样点
    local = seg_abs[min_pos : min_pos + win]
    inner = np.argmin(local)
    
    boundary_sample = left + min_pos + inner
    return align_pcm_bytes(boundary_sample * 2)
```

**关键设计**: 120ms 窗口确保切分点在 **至少 120ms 的低能量区间** 内，这通常对应字词间的微停顿或呼吸间隙，而不是元音或辅音中间。

### 3.3 阶段二: 语义提交决策 (`_semantic_commit_from_result`)

这是整个算法最核心的部分。当 ASR 推理返回带时间戳的 segments 时，算法扫描这些 segments 寻找最佳语义切分点。

#### 输入数据结构

```python
# ASR 返回的 segments 示例 (Forced Aligner 逐字/逐词时间戳):
segments = [
    {"start": 0.0,  "end": 0.3,  "text": "今天"},
    {"start": 0.3,  "end": 0.6,  "text": "的"},
    {"start": 0.6,  "end": 1.2,  "text": "会议"},
    {"start": 1.5,  "end": 1.8,  "text": "主要"},
    {"start": 1.8,  "end": 2.4,  "text": "讨论"},
    {"start": 2.4,  "end": 2.7,  "text": "三个"},
    {"start": 2.7,  "end": 3.3,  "text": "方面。"},  ← 强边界标点
    {"start": 3.8,  "end": 4.2,  "text": "第一"},
    {"start": 4.2,  "end": 4.8,  "text": "是"},
    ...
]
```

#### 切分决策算法

```python
def _semantic_commit_from_result(result, boundary, final):
    segments = normalize_timed_segments(result.segments, decode_duration)
    
    # 安全边界: 不在音频末尾 0.35s 范围内切分
    # (末尾可能有不完整的词，模型还没生成完)
    search_limit = decode_duration - semantic_right_guard_sec  # -0.35s
    
    latest_candidate_idx = None  # 最后一个合法切分点
    visible_chars = 0            # 累积可见字符数
    
    for index, segment in enumerate(segments):
        token_end = segment["end"]
        
        # 超过安全边界 → 停止搜索
        if token_end > search_limit:
            break
        
        token_text = segment["text"].strip()
        visible_chars += len(token_text)
        
        # 计算当前 segment 与下一个 segment 之间的时间间隔
        next_start = segments[index + 1]["start"] if index + 1 < len(segments) else decode_duration
        gap_sec = next_start - token_end
        
        # ── 四级语义边界判定 ──────────────────────────────
        
        # 级别 1: 强语义边界 (句末标点)
        # 。！？!?…）)
        if is_strong_semantic_boundary(token_text):
            latest_candidate_idx = index
        
        # 级别 2: 软语义边界 (逗号等) + 停顿 >= 0.6s
        # ，,、；;：:  且后面有 >= 600ms 的间隔
        elif is_soft_semantic_boundary(token_text) and gap_sec >= 0.6:
            latest_candidate_idx = index
        
        # 级别 3: 长停顿 (>= 0.45s) 且已有足够文本 (>= 6 字符)
        elif gap_sec >= 0.45 and visible_chars >= 6:
            latest_candidate_idx = index
        
        # 级别 4: 短停顿 (>= 1.2s) 且是短语音 (<= 3 字符)
        # 处理 "嗯"、"好的" 等极短回应
        elif gap_sec >= 1.2 and 0 < visible_chars <= 3:
            latest_candidate_idx = index
    
    # ── 找不到切分点 ──────────────────────────────────
    if latest_candidate_idx is None:
        if final:
            # flush 模式: 全部提交
            return SemanticCommit(text=all_text, committed_bytes=boundary)
        
        if decode_duration >= 18.0 and visible_chars >= 6:
            # 超时强制提交 (防止无限等待)
            latest_candidate_idx = last_idx_before_limit
        else:
            # 继续等待更多音频
            return SemanticCommit(should_wait=True)
    
    # ── 构造提交结果 ──────────────────────────────────
    commit_text = "".join(s["text"] for s in segments[:latest_candidate_idx + 1])
    commit_sec  = segments[latest_candidate_idx]["end"]
    commit_bytes = round(commit_sec * bytes_per_second)
    
    return SemanticCommit(
        text=commit_text,
        committed_bytes=commit_bytes,  # 精确到音频字节位置
        audio_duration=commit_bytes / bytes_per_second,
    )
```

### 3.4 四级语义边界判定详解

```
优先级          条件                             典型场景
─────────────────────────────────────────────────────────────────
级别 1 (最强)   句末标点: 。！？!?…）)           "会议讨论了三个方面。"
                无需间隔                          → 在 "。" 后切分

级别 2 (强)     逗号类标点 + gap >= 0.6s          "首先，(0.8s停顿) 我们..."
                ，,、；;：:                        → 在 "，" 后切分

级别 3 (中)     任意位置 + gap >= 0.45s           "讨论成本控制 (0.5s) 然后..."
                + 已有 >= 6 字符                   → 在 "控制" 后切分

级别 4 (弱)     短回应 + gap >= 1.2s              "嗯 (1.5s停顿)"
                1-3 字符                           → 在 "嗯" 后切分

强制提交         decode_duration >= 18s            连续说话 18s 无任何停顿
                 + >= 6 字符                       → 强制在安全边界内切分
```

### 3.5 安全机制

#### Right Guard (右侧保护带)

```
|←────── 已解码音频 ──────────────────────→|
|                                          |
|  可搜索区域                    | 0.35s   |
|  (寻找切分点)                  | 保护带   |
|                                |         |
0                         search_limit   decode_end

保护带的作用: 模型可能还没来得及生成最后 0.35s 对应的完整 token，
如果在此处切分，可能丢失半个词。
```

#### 文本去重 (`_deduplicate_text`)

由于相邻 decode 窗口有 overlap (1.5s)，同一段音频会被推理两次。去重机制:

```python
def _deduplicate_text(text):
    # 保留最近 120 个字符的历史
    tail = recent_text_tail[-120:]
    
    # 标准化: 只保留字母数字和 CJK 字符，忽略标点和空格
    # 例: "讨论了三个方面。" → "讨论了三个方面"
    # 例: "Hello, world!"   → "helloworld"
    
    # 寻找 tail 的后缀与 text 的前缀的最长匹配
    overlap_chars = find_normalized_overlap(tail, text)
    
    if overlap_chars > 0:
        # 裁剪掉重复部分
        text = text[trim_index:]
    
    return text
```

**示例**:

```
上一段输出: "...讨论了三个方面。"
本段推理  : "三个方面。第一是成本控制..."
                         ↑
标准化匹配: "三个方面" 重叠
去重结果  : "第一是成本控制..."
```

### 3.6 缓冲区管理: Overlap 滑动窗口

```
时间轴 ──────────────────────────────────────────────────────────>

第 1 轮推理:
|←─── boundary ──────────────→|
|  chunk 1 音频               |
|  推理 + 语义提交             |
|                   |← 1.5s →|
|                   | overlap |
                    ↓
第 2 轮推理:
                    |←─── 新音频 ──────→|
                    |←overlap→|←新数据→ |
                    | (保留的上下文)     |
                    | 已解码但保留       |

overlap 的作用:
1. 上下文连续性: 下一段推理能"看到"上一段的尾部
2. 边界恢复: 如果切分点恰好在一个词中间，overlap 确保下一段能完整识别该词
3. 去重处理: _deduplicate_text() 在输出层去除重复部分
```

---

## 4. 上传路径: 能量最小值分段 + 时间戳后分段

> 文件: `mlx_transcriber.py` (能量分段) + `asr_service.py` (时间戳后分段)

### 4.1 宏观分段 (能量最小值切分)

将长音频按 ~120s 切分成 chunks，切分点选在能量最低处:

```python
# mlx_transcriber.py: _split_energy_chunks()
def _split_energy_chunks(audio):
    if total_sec <= 120:
        return [(audio, 0.0)]  # 不分段
    
    chunks = []
    start = 0
    
    while start < len(audio):
        target_cut = start + 120s * sr
        
        # 在 target_cut ± 15s 范围内搜索
        left  = max(start, target_cut - 15s * sr)
        right = min(len(audio), target_cut + 15s * sr)
        
        # 50ms 窗口计算二次能量
        energy = convolve(seg², ones(50ms_window), mode="valid")
        
        # 选最低能量点
        cut_sample = left + argmin(energy) + win // 2
        
        chunks.append((audio[start:cut_sample], start / sr))
        start = cut_sample
    
    return chunks
```

### 4.2 微观分段 (Forced Aligner 时间戳后分段)

每个 ~120s chunk 经 ASR 推理后，如果有 Forced Aligner，会得到逐字/逐词时间戳。
`_extract_segments_from_timestamps` 再根据语义信号二次切分:

```python
# asr_service.py: _extract_segments_from_timestamps()
# 切分规则:
SEMANTIC_PAUSE_SEC = 0.3       # 普通停顿阈值
SEMANTIC_SOFT_PAUSE_SEC = 0.5  # 逗号类停顿阈值
MAX_SEG_SEC = 10.0             # 单段最大时长

for each gap between consecutive aligner items:
    if 前一项以 。！？!?…）) 结尾:        → 切分 (强边界)
    elif 前一项以 ，,、；;：: 结尾 且 gap >= 0.5s: → 切分 (软边界)
    elif gap >= 0.3s:                       → 切分 (停顿边界)
    elif seg_duration >= 10s:               → 切分 (时长上限)
```

---

## 5. 三种分段策略对比

| 维度 | WebRTC VAD (当前实时) | TransformerAudioChunker (语义) | 上传能量分段 |
|------|----------------------|-------------------------------|-------------|
| **切分信号** | 声学能量 (VAD 逐帧) | ASR 时间戳 + 标点 + 停顿 | 滑动窗口能量 + Aligner |
| **延迟** | ~720ms (36帧×20ms) | 可变 (4-18s 缓冲) | N/A (离线) |
| **语义感知** | 无 | 有 (标点 + 间隔分析) | 有 (后处理) |
| **准确度** | 中 (常切碎句子) | 高 (尊重句子边界) | 高 (宏观不影响) |
| **计算开销** | 极低 (C++ VAD) | 高 (每段需完整 ASR 推理) | 中 (批量推理) |
| **适用模型** | 任何 ASR | 需要 Forced Aligner | 需要 Forced Aligner |
| **重复处理** | 不需要 | 需要 (overlap 去重) | 不需要 |
| **当前状态** | 生产使用中 | 代码完备，未启用 | 生产使用中 |

---

## 6. 当前系统的准确度瓶颈分析

### 6.1 实时路径: VAD 切分的语义割裂

**问题**: WebRTC VAD 只看声学能量，在以下场景表现差:

```
场景 1: 思考停顿
发言: "这个方案呢...嗯...我觉得从技术角度来看..."
         [720ms静音]    [800ms静音]
VAD 输出:
  段1: "这个方案呢"
  段2: "嗯"
  段3: "我觉得从技术角度来看"
→ 3 段分别送 ASR，上下文丢失

场景 2: 连续长发言
发言: 一口气说 25 秒 (技术讨论)
VAD 输出:
  段1: 前 18s (max_utterance_sec 强制切断)
  段2: 后 7s
→ 可能切在句子甚至词语中间

场景 3: 嘈杂环境
环境: 键盘声、纸张声、空调声
VAD 输出:
  各种碎片: "咔", "沙沙", 空文本...
→ 产生大量无意义短段落
```

### 6.2 Forced Aligner 缺失时的退化

当 `FINAL_ASR_ALIGNER_PATH` 未配置或路径无效时:
- `has_timestamps` = `False`
- TransformerAudioChunker 退化为纯能量边界切分 (无语义)
- 上传路径退化为每个 120s chunk 输出单个段落 (无细分)

### 6.3 实时语义路径未启用

`TransformerAudioChunker` 虽然代码完备，但当前 `server.py` 中:

```python
def build_realtime_transcriber(session_state):
    router = FinalOnlyASRRouter(asr_service, ...)
    return WebRTCVADMeetingTranscriber(router=router, ...)  # ← 使用 VAD，而非语义
```

`FinalOnlyASRRouter.transcribe_final()` 调用时设置 `extract_timestamps=False`，即使启用语义路径也没有时间戳。

### 6.4 CJK 文本特殊挑战

- 中日韩文本无空格分词，切分点对语义影响更大
- "今天下午" 切在 "今天" 和 "下午" 之间语义完整
- "今天下" 和 "午" 则完全错误
- 当前 VAD 无法区分这些情况

---

## 7. 提高准确度的改进方案

### 方案 1: 启用 VAD + 语义混合路径 (推荐，改动最小)

**思路**: 保持 WebRTC VAD 做快速语音检测和端点判定，但不直接将 VAD 切分的音频送 ASR。
改为将 VAD 判定的语音段落喂给 `TransformerAudioChunker`，让语义层做最终切分。

```
当前:  VAD 切分 → ASR 推理 → 发射
改进:  VAD 检测语音 → 音频累积 → ASR 推理 (带时间戳) → 语义切分 → 发射
```

**具体改动**:

```python
# server.py 中修改 build_realtime_transcriber:
def build_realtime_transcriber(session_state):
    router = FinalOnlyASRRouter(
        asr_service,
        language_getter=...,
        context_getter=...,
    )
    # 改: 启用时间戳提取
    # router.transcribe_final() 需要 extract_timestamps=True
    
    # 使用 SemanticMeetingTranscriber 替代 WebRTCVADMeetingTranscriber
    config = build_transformer_chunking_config()
    return SemanticMeetingTranscriber(
        transcribe_wav=router.transcribe_final,
        sample_rate=asr_config.sample_rate,
        config=config,
    )
```

**同时需要修改** `FinalOnlyASRRouter`:
```python
async def transcribe_final(self, audio_tuple) -> ASRResult:  # 改返回类型
    result = await self._final_service.transcribe_wav(
        audio_tuple,
        language=self._language_getter(),
        context=context,
        extract_timestamps=True,  # ← 启用时间戳
    )
    return result  # 保留完整 ASRResult (含 segments + has_timestamps)
```

**注意**: 这需要 Forced Aligner 模型 (`Qwen3-ForcedAligner-0.6B`) 已加载。

**优点**:
- 利用已有代码，改动量最小
- 语义切分质量最高 (基于真实时间戳和标点)
- 支持四级语义边界判定

**缺点**:
- 延迟增加: 需要累积 4-12s 音频才能做一次推理
- 计算开销增加: Forced Aligner 额外推理
- 首字延迟: 用户说完第一句话后需要等更久才能看到文本

**预估影响**:
- 首字延迟: 从 ~0.7s 增加到 ~4-5s
- 分段准确度: 从 ~60% 提升到 ~90%+ (句子级)

---

### 方案 2: VAD 端点 + 后处理合并 (中等改动)

**思路**: 保持 VAD 快速输出，但在 WebSocket 发射层增加"段落合并"逻辑，根据 ASR 输出的标点和时间关系，将被 VAD 切碎的段落重新合并。

```
VAD 切分 → ASR 推理 → 段落合并器 → 发射
                         │
                         ├─ 规则 1: 如果段落不以句末标点结尾，
                         │         且与下一段间隔 < 1.5s → 合并
                         │
                         ├─ 规则 2: 如果段落 < 3 字符 (如 "嗯")，
                         │         且与前/后段间隔 < 2s → 合并
                         │
                         └─ 规则 3: 合并后段落不超过 30s
```

**实现草案**:

```python
class SegmentMerger:
    """后处理段落合并器"""
    
    def __init__(self, max_gap_sec=1.5, max_merged_sec=30.0):
        self.max_gap_sec = max_gap_sec
        self.max_merged_sec = max_merged_sec
        self._pending: Optional[TranscriptSegment] = None
    
    STRONG_BOUNDARIES = set("。！？!?…）)")
    
    def push(self, segment) -> Optional[TranscriptSegment]:
        """推入一个段落，返回可发射的合并结果 (如果有)。"""
        if self._pending is None:
            self._pending = segment
            return None
        
        gap = segment.start_time - self._pending.end_time
        merged_duration = segment.end_time - self._pending.start_time
        ends_with_boundary = any(
            self._pending.text.rstrip().endswith(p) for p in self.STRONG_BOUNDARIES
        )
        
        # 决定是否合并
        should_merge = (
            not ends_with_boundary
            and gap < self.max_gap_sec
            and merged_duration < self.max_merged_sec
        )
        
        if should_merge:
            # 合并
            self._pending = merge(self._pending, segment)
            return None
        else:
            # 发射 pending，缓存新段落
            result = self._pending
            self._pending = segment
            return result
    
    def flush(self) -> Optional[TranscriptSegment]:
        result = self._pending
        self._pending = None
        return result
```

**优点**:
- 保持低延迟 (VAD 快速输出)
- 不需要 Forced Aligner
- 改动集中在输出层，不影响 ASR 推理

**缺点**:
- 合并质量依赖启发式规则
- 无法解决 "词语中间切断" 的问题 (已经切了)
- 合并后的段落时间戳是近似值

**预估影响**:
- 首字延迟: 保持 ~0.7s (但发射延迟增加 1-2s 等待合并判定)
- 分段准确度: 从 ~60% 提升到 ~75% (段落级合并，但不解决词级问题)

---

### 方案 3: 双轨并行 — 快速预览 + 精确最终版 (最优体验，改动较大)

**思路**: 同时运行两条路径:
- **快速轨**: VAD → 轻量 ASR → 即时预览 (preview)
- **精确轨**: 语义分段 → 完整 ASR → 最终提交 (final)

用户先看到预览文本（低延迟），几秒后被精确版本替换。

```
音频流 ─┬──→ WebRTC VAD → 快速 ASR → preview transcript (灰色)
        │                                    │
        │                                    │ 2-5s 后
        │                                    ▼
        └──→ TransformerAudioChunker → 精确 ASR → final transcript (替换)
```

**WebSocket 消息扩展**:

```json
// 预览 (快速但不精确)
{
  "type": "transcript",
  "segment": {
    "segment_id": 5,
    "revision": 1,
    "is_final": false,
    "text": "这个方案"
  }
}

// 最终版 (精确，替换预览)
{
  "type": "transcript",
  "segment": {
    "segment_id": 5,
    "revision": 2,
    "is_final": true,
    "text": "这个方案呢，嗯，我觉得从技术角度来看是可行的。"
  }
}
```

**优点**:
- 用户体验最佳: 即时看到文本，几秒后自动纠正
- 分段精度最高: 语义路径决定最终结果
- 渐进式增强: 网络差时至少有预览

**缺点**:
- 计算量翻倍 (两次 ASR 推理)
- 前端需要支持 revision 替换逻辑
- 实现复杂度最高

**预估影响**:
- 首字延迟: ~0.7s (preview) + ~4-5s (final 替换)
- 分段准确度: ~90%+ (final 版本)
- GPU/CPU 负载: +80-100%

---

### 方案 4: 参数调优 (零改动，立即生效)

不改代码，仅调整现有 VAD 参数:

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `endpoint_silence_frames` | 36 (720ms) | **50** (1000ms) | 容忍更长停顿，减少句内切断 |
| `max_utterance_sec` | 18.0 | **25.0** | 容纳更长的完整句子 |
| `vad_aggressiveness` | 2 | **1** | 安静环境下更少误判静音 |
| `enter_speech_frames` | 2 | **3** | 减少噪声误触发 |

```bash
# .env 修改
WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES=50
WEBRTC_VAD_MAX_UTTERANCE_SEC=25
WEBRTC_VAD_AGGRESSIVENESS=1
WEBRTC_VAD_ENTER_SPEECH_FRAMES=3
```

**优点**: 零改动，立即测试
**缺点**: 治标不治本，延迟增加

---

### 方案优先级建议

```
推荐实施顺序:

1. 方案 4 (参数调优)     — 立即: 5 分钟完成，验证基线改善
2. 方案 1 (启用语义路径)  — 短期: 启用已有代码，需确保 Aligner 加载
3. 方案 2 (后处理合并)    — 中期: 作为方案 1 的补充，处理无 Aligner 场景
4. 方案 3 (双轨并行)      — 长期: 最优体验，但投入最大
```

---

## 附录 A: TransformerChunkingConfig 完整参数表

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|---------|------|
| `min_chunk_sec` | 4.0 | `ASR_MIN_CHUNK_SEC` | 最小音频累积时长 |
| `preferred_chunk_sec` | 8.0 | `ASR_PREFERRED_CHUNK_SEC` | 理想分段长度 |
| `max_chunk_sec` | 12.0 | `ASR_MAX_CHUNK_SEC` | 硬上限触发分段 |
| `overlap_sec` | 1.5 | `ASR_OVERLAP_SEC` | 相邻 decode 重叠时长 |
| `endpoint_silence_sec` | 0.45 | `ASR_ENDPOINT_SILENCE_SEC` | 尾部静音检测阈值 |
| `boundary_search_sec` | 1.25 | `ASR_BOUNDARY_SEARCH_SEC` | 能量搜索窗口范围 |
| `energy_window_ms` | 120.0 | — | 滑动能量窗口宽度 |
| `silence_threshold` | 0.012 | — | 静音判定阈值 (归一化幅度) |
| `semantic_pause_sec` | 0.45 | `ASR_SEMANTIC_PAUSE_SEC` | 语义停顿阈值 (级别3) |
| `semantic_soft_pause_sec` | 0.6 | `ASR_SEMANTIC_SOFT_PAUSE_SEC` | 软边界停顿阈值 (级别2) |
| `semantic_short_pause_sec` | 1.2 | — | 短语音停顿阈值 (级别4) |
| `semantic_right_guard_sec` | 0.35 | — | 右侧保护带宽度 |
| `semantic_force_commit_sec` | 18.0 | `ASR_SEMANTIC_FORCE_COMMIT_SEC` | 强制提交超时 |
| `semantic_min_chars` | 6 | — | 级别3最小字符要求 |
| `semantic_short_utterance_chars` | 3 | — | 级别4短语音字符上限 |
| `dedupe_tail_chars` | 120 | — | 去重历史窗口 |
| `min_dedupe_overlap_chars` | 4 | — | 最小重叠匹配长度 |

## 附录 B: 语义边界标点定义

| 类型 | 字符集 | 触发条件 |
|------|--------|---------|
| **强边界** | `。` `！` `？` `!` `?` `…` `）` `)` | 无条件切分 (级别1) |
| **软边界** | `，` `,` `、` `；` `;` `：` `:` | 需配合 >= 0.6s 停顿 (级别2) |
| **停顿边界** | 任意字符 | 需 >= 0.45s gap + >= 6 字符 (级别3) |
| **短语音边界** | 1-3 字符 | 需 >= 1.2s gap (级别4) |
