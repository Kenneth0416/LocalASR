# MeetScribe UI 迁移设计文档

**日期:** 2026-04-21  
**项目:** meeting_realtime_voice 前端迁移  
**范围:** Phase 1 — 核心 UI 迁移（最小可用，4 页面）

---

## 1. 项目背景与目标

### 1.1 现状
- **后端:** Python FastAPI，提供实时语音转录、会议 CRUD、文件上传、WebSocket、AI 聊天等 API
- **现有前端:** 单页 `index.html` + `app.js`（纯 JS/DOM 操作），功能集中在实时转录 + 上传 + 历史查看
- **新 UI 设计:** `Real Time Meeting Transription/` 目录中一套完整的多页 React UI 原型（Dashboard、Meeting Scribe、Upload、Library、Actions、Settings、Login 等）
- **API 文档:** 新 UI 配套了一套完整的前后端 API 规范，但现有后端未完全实现（缺少认证、用户系统、行动项目、多模板摘要等）

### 1.2 目标
将新设计的 MeetScribe UI 迁移到现有 FastAPI 后端之上，分阶段实现。Phase 1 仅对接现有后端已支持的 API，缺失功能隐藏对应的 UI。

### 1.3 成功标准
- [ ] Dashboard 页面能展示会议统计和最近会议列表
- [ ] MeetingRoom 页面能正常实时转录 + 问答聊天
- [ ] Upload 页面能上传音频并展示转录结果
- [ ] Library 页面能浏览、搜索、删除历史会议
- [ ] 所有页面对接真实 API，非 mock 数据
- [ ] 旧的 `static/` 目录被新 Vite 构建产物完全替换
- [ ] 移动端响应式正常

---

## 2. 架构设计

### 2.1 目录结构

```
meeting_realtime_voice/
├── backend/              ← 现有 Python 代码不变
│   ├── server.py
│   ├── http_endpoints.py
│   ├── ws_handler.py
│   ├── persistence.py
│   ├── session.py
│   ├── asr.py
│   └── ...
├── frontend/             ← 新 Vite React 项目
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Dashboard.jsx
│   │   │   ├── MeetingRoom.jsx
│   │   │   ├── Upload.jsx
│   │   │   └── Library.jsx
│   │   ├── components/
│   │   │   ├── Sidebar.jsx
│   │   │   ├── Header.jsx
│   │   │   ├── TranscriptPanel.jsx
│   │   │   ├── ChatPanel.jsx
│   │   │   ├── SummaryPanel.jsx
│   │   │   ├── UploadDropzone.jsx
│   │   │   ├── MeetingCard.jsx
│   │   │   └── StatusBadge.jsx
│   │   ├── hooks/
│   │   │   ├── useApi.js
│   │   │   └── useWebSocket.js
│   │   ├── api/
│   │   │   └── client.js
│   │   ├── utils/
│   │   │   └── formatters.js
│   │   ├── styles/
│   │   │   └── theme.css
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
└── static/               ← Vite 构建输出（被 FastAPI serve）
```

### 2.2 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 构建工具 | Vite | 快速 HMR、优化构建 |
| 前端框架 | React 18 | 函数组件 + Hooks |
| 路由 | React Router v6 | 客户端路由（BrowserRouter） |
| HTTP 客户端 | Axios | 统一拦截错误、设置 baseURL |
| 样式 | CSS Modules / CSS Variables | 复用新 UI 的设计系统色值 |
| 图标 | Lucide React | SVG 图标 |

### 2.3 路由配置

React Router 做客户端路由，FastAPI 后端增加 catch-all 路由：

```
// React Router 路由表
/           → Dashboard
/live       → MeetingRoom
/upload     → Upload
/library    → Library

// FastAPI catch-all
@app.get("/{path:path}")
async def serve_spa(path: str):
    """Serve index.html for all non-API routes to support client-side routing."""
    return FileResponse(str(STATIC_DIR / "index.html"))
```

---

## 3. 页面 ↔ API 映射

### 3.1 Phase 1 实现的页面（对接真实 API）

| 页面 | 路由 | 对接的 API | 功能 |
|------|------|-----------|------|
| Dashboard | `/` | `GET /api/health`, `GET /api/meetings` | 系统状态卡片、最近会议列表、快速操作入口 |
| MeetingRoom | `/live` | `WS /ws`, `POST /api/meetings/{id}/chat` | 实时转录、AI 问答聊天 |
| Upload | `/upload` | `POST /api/upload`, `GET /api/meetings/{id}` | 音频文件上传、转录进度、结果展示 |
| Library | `/library` | `GET /api/meetings`, `GET /api/meetings/{id}`, `DELETE /api/meetings/{id}`, `GET /api/meetings/{id}/export.*` | 会议列表、搜索、详情、删除、导出 |

### 3.2 Phase 1 隐藏的页面（无后端支持）

| 页面 | 隐藏原因 | Phase 2 需补充的后端功能 |
|------|---------|--------------------------|
| Login | 无认证系统 | JWT 认证、用户注册/登录 API |
| Settings | 无用户系统、无配置持久化 | 用户偏好存储、系统配置 API |
| Actions | 无 action_items API | 行动项目 CRUD API |
| Review | 依赖行动项目 | — |
| System Map | 仅设计文档页面 | — |

---

## 4. 数据流

### 4.1 MeetingRoom 实时转录数据流

```
用户点击"开始新会议"
  → 前端建立 WebSocket 连接 (ws://host/ws)
  → 后端创建 session，返回 session_id
  → 前端开始 capture 麦克风音频 (MediaRecorder / Web Audio API)
  → 音频 chunks 通过 WebSocket 发送到后端
  → 后端 ASR 处理 → 推送 transcript 消息到前端
  → 前端更新 TranscriptPanel 的 transcript 列表

用户发送聊天消息
  → 前端 POST /api/meetings/{session_id}/chat { question }
  → 后端 LLM 处理 → 返回流式响应 (SSE)
  → 前端在 ChatPanel 中逐字渲染回答
```

### 4.2 Upload 上传转录数据流

```
用户选择音频文件
  → 前端 POST /api/upload (multipart/form-data)
  → 后端判断音频长度：
      → 短音频：直接 ASR 转录
      → 长音频：VAD chunk 批量处理
  → 后端创建 session + meeting_store.complete_session()
  → 如果 segment >= 3：异步触发 summary 生成
  → 前端收到响应（session_id、segments、summary_state）
  → 前端跳转 Library 或展示结果
```

### 4.3 Library 浏览数据流

```
页面加载
  → GET /api/meetings → 会议列表
  → 前端本地搜索/筛选（后端暂不支持分页和搜索参数，前端本地过滤）

用户点击会议
  → GET /api/meetings/{id} → 会议详情（transcript、summary、chat）
  → 展示在详情面板

用户点击导出
  → GET /api/meetings/{id}/export.txt|md|json → 下载文件

用户删除会议
  → DELETE /api/meetings/{id} → 刷新列表
```

---

## 5. 组件设计

### 5.1 Layout 组件

| 组件 | 职责 | Props |
|------|------|-------|
| `AppShell` | 整体布局：Sidebar + 主内容区 | children |
| `Sidebar` | 导航菜单（4 项）、Logo、主题切换 | activePage |
| `Header` | 页面标题、状态徽章、控制按钮 | title, status, actions |

### 5.2 页面级组件

| 组件 | 职责 | 关键子组件 |
|------|------|-----------|
| `Dashboard` | 统计卡片、最近会议、快速操作 | StatsGrid, MeetingCard, QuickAction |
| `MeetingRoom` | 实时转录主页面 | TranscriptPanel, ChatPanel, SummaryPanel, AudioControls |
| `Upload` | 文件上传 + 结果展示 | UploadDropzone, UploadProgress, ResultPreview |
| `Library` | 会议列表 + 详情 | MeetingList, MeetingDetailPanel, ExportMenu |

### 5.3 共享组件

| 组件 | 职责 |
|------|------|
| `MeetingCard` | 会议列表项（标题、日期、时长、状态、行动项目数） |
| `StatusBadge` | 状态徽章（live / completed / processing / failed） |
| `TranscriptSegment` | 单条转录（时间戳、说话人、文本、编辑按钮） |
| `ChatMessage` | 聊天消息（用户/AI、时间戳） |
| `EmptyState` | 空状态插画 + 引导文案 |
| `Toast` | 全局通知（成功/错误/警告） |

---

## 6. 状态管理

Phase 1 采用 **React Context + useReducer**，无需 Redux：

- `MeetingContext` — 当前会议状态（session_id、transcript、chat、summary）
- `WebSocketContext` — WebSocket 连接状态、消息队列
- `UploadContext` — 上传文件状态、进度、结果
- `LibraryContext` — 会议列表、搜索关键词、选中会议

---

## 7. API 客户端封装

```javascript
// api/client.js
import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' }
});

// 统一错误拦截
api.interceptors.response.use(
  res => res,
  err => {
    // 4xx/5xx → Toast 通知
    return Promise.reject(err);
  }
);

export default api;
```

---

## 8. WebSocket 封装

```javascript
// hooks/useWebSocket.js
// 职责：建立 WS 连接、心跳检测、自动重连、消息分发
// 输入：session_id（可选，新建时为 null）
// 输出：{ isConnected, sendAudio, sendMessage, transcript, error, reconnect }
```

- 使用原生 WebSocket API（不引入 Socket.io）
- 心跳：每 30s 发送 ping/pong
- 断线：3 秒内自动重连，最多 5 次
- 音频格式：PCM 16kHz mono（复用现有 audio-worklet.js 逻辑）

---

## 9. 样式系统

复用新 UI 的深色主题色值（CSS Variables）：

```css
:root {
  --bg: #0F0E0C;
  --surface: #191714;
  --surface2: #22201C;
  --surface3: #2C2924;
  --border: #332F28;
  --accent: #3B82F6;
  --accent-dim: #3B82F620;
  --accent2: #818CF8;
  --text: #F5F0EB;
  --text-muted: #7A7060;
  --red: #EF4444;
  --green: #22C55E;
  --yellow: #F59E0B;
  --font-ui: 'Space Grotesk', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
```

浅色主题通过 `.light` class 切换，用 CSS transition 做平滑过渡。

---

## 10. 错误处理策略

| 场景 | 处理方式 |
|------|----------|
| API 4xx | Toast 提示具体错误（如"会议不存在"） |
| API 5xx | Toast "服务器错误，请稍后重试" |
| WebSocket 断开 | 顶部 Banner 提示 + 重连按钮 |
| 上传转录失败 | 进度条变红 + 错误详情 + 重新上传 |
| 无会议历史 | EmptyState 插画 + "开始新会议"引导按钮 |
| 网络超时 | 自动重试 1 次，仍失败则 Toast |

---

## 11. 安全性考量

- WebSocket 连接在 HTTPS 环境下自动使用 WSS
- 文件上传限制：前端检查 MIME type（audio/*），后端已校验
- 导出文件：后端已正确处理 Content-Disposition
- XSS 防护：React 自动转义 JSX 插值，Markdown 渲染用 DOMPurify

---

## 12. 测试策略

| 类型 | 范围 | 工具 |
|------|------|------|
| 组件测试 | 共享组件（MeetingCard、StatusBadge 等） | Vitest + React Testing Library |
| 集成测试 | API 调用、WebSocket 连接 | Vitest + MSW（mock service worker） |
| E2E 测试 | 核心用户流程 | Playwright |

---

## 13. Phase 2 规划（概要）

Phase 2 将在后端补充以下功能后，逐个解锁隐藏页面：

1. **认证系统** — JWT login/register/refresh → 解锁 Login + Settings
2. **用户系统** — 用户 CRUD + 偏好存储 → 解锁 Sidebar 用户区域
3. **行动项目 API** — action_items CRUD → 解锁 Actions + Review 页面
4. **多模板摘要** — `/meetings/{id}/summaries` → SummaryPanel 增加模板选择
5. **搜索/筛选** — 后端支持分页和搜索参数 → Library 增强搜索体验

---

## 14. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 旧前端有复杂的音频 worklet 逻辑 | 高 | 逐步迁移，保留 audio-worklet.js 并在 React 中引用 |
| 新 UI 设计是原型级别，可能有交互遗漏 | 中 | 与现有前端功能逐项对比，确保不丢功能 |
| FastAPI catch-all 可能覆盖 API 路由 | 高 | 确保 catch-all 放在最后，或排除 `/api` 和 `/static` |
| 构建产物体积大 | 低 | Vite 默认 tree-shaking + code splitting |

---

## 附录 A：现有 API 端点清单（Phase 1 对接）

```
GET    /api/health              → Dashboard 系统状态
GET    /api/readiness           → Dashboard 就绪状态
GET    /api/pipeline            → MeetingRoom pipeline 调试

GET    /api/meetings            → Library 列表, Dashboard 最近会议
GET    /api/meetings/{id}       → Library 详情
DELETE /api/meetings/{id}       → Library 删除
PATCH  /api/meetings/{id}/transcript/{segId} → MeetingRoom 转录编辑
POST   /api/meetings/{id}/chat  → MeetingRoom AI 问答
POST   /api/meetings/{id}/summary → MeetingRoom 重新生成摘要

GET    /api/meetings/{id}/export.txt  → Library 导出
GET    /api/meetings/{id}/export.md   → Library 导出
GET    /api/meetings/{id}/export.json → Library 导出
GET    /api/meetings/{id}/recording   → Library 下载录音

POST   /api/upload              → Upload 文件上传

WS     /ws                      → MeetingRoom 实时转录
```

## 附录 B：新旧 UI 功能对照

| 功能 | 旧前端 | 新 UI (Phase 1) | 新 UI (Phase 2) |
|------|--------|----------------|----------------|
| 实时转录 | 有 | MeetingRoom | — |
| 文件上传 | 有 | Upload | — |
| 历史查看 | 有 (drawer) | Library | — |
| AI 问答 | 有 | MeetingRoom | — |
| 摘要生成 | 有 | MeetingRoom | — |
| 转录编辑 | 有 | MeetingRoom | — |
| 导出 | 有 | Library | — |
| Dashboard | 无 | Dashboard | — |
| 用户认证 | 无 | 隐藏 | Login |
| 行动项目 | 无 | 隐藏 | Actions |
| 多模板摘要 | 无 | 隐藏 | Review |
| 设置 | 无 | 隐藏 | Settings |
