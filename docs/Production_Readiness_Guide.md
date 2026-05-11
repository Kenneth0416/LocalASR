# MeetScribe 后端生产化改造方案

> 最后更新: 2026-04-22 | 现状: 单机开发级 → 目标: 生产级部署

---

## 目录

1. [现状评估摘要](#1-现状评估摘要)
2. [改造优先级矩阵](#2-改造优先级矩阵)
3. [Phase 1: 安全与数据完整性 (P0 — 必须做)](#phase-1-安全与数据完整性-p0--必须做)
4. [Phase 2: 可靠性与稳定性 (P1 — 强烈建议)](#phase-2-可靠性与稳定性-p1--强烈建议)
5. [Phase 3: 可观测性 (P2 — 生产运维必备)](#phase-3-可观测性-p2--生产运维必备)
6. [Phase 4: 性能与扩展性 (P3 — 按需)](#phase-4-性能与扩展性-p3--按需)
7. [Phase 5: 运维自动化 (P3 — 按需)](#phase-5-运维自动化-p3--按需)
8. [改造总览时间线](#6-改造总览时间线)

---

## 1. 现状评估摘要

### 已达标 (Production-Ready)

| 组件 | 状态 | 说明 |
|------|------|------|
| 配置系统 | OK | dataclass + 环境变量，类型转换 |
| Docker 部署 | OK | Dockerfile + docker-compose，含 Ollama + 健康检查 |
| 健康检查 | OK | `/api/health` + `/api/readiness` |
| 导出功能 | OK | 文本/Markdown/JSON 三种格式 |
| 异步架构 | OK | 全链路 asyncio |
| 测试覆盖 | OK | ~6900 行测试代码，CI 持续集成 |
| 文件校验 | OK | 上传类型 + 500MB 大小限制 |

### 不达标 (Not Production-Ready)

| 领域 | 严重程度 | 核心问题 |
|------|---------|---------|
| 认证鉴权 | **致命** | 零认证，任何人均可访问所有 API |
| 数据库 | **致命** | 无 WAL 模式、无连接池、无备份、无迁移 |
| 安全 | **高** | .env 含密钥在 git 中、无输入消毒、无 SQL 注入防护 |
| 错误处理 | **高** | 异常被吞、响应格式不统一 |
| 日志 | **中** | 无结构化日志、无请求追踪 |
| 并发安全 | **中** | Session dict 无锁、WebSocket 无限连接 |
| API 设计 | **中** | 无版本号、无分页、无限速 |
| 文件管理 | **低** | 无过期清理、无压缩、临时文件泄漏 |
| WebSocket | **中** | 无服务端心跳、无背压监控、无连接限制 |

---

## 2. 改造优先级矩阵

```
紧急且重要 (立即做)          重要不紧急 (短期内做)
┌────────────────────────┐  ┌────────────────────────┐
│ P0 认证鉴权              │  │ P1 结构化日志            │
│ P0 SQLite WAL + 连接池    │  │ P1 请求追踪 (trace_id)   │
│ P0 .env 安全 / 密钥管理   │  │ P1 统一错误处理          │
│ P0 输入消毒 / SQL 注入防护 │  │ P1 数据库备份            │
│ P0 Session 并发安全       │  │ P1 API 版本化            │
└────────────────────────┘  └────────────────────────┘

紧急不重要 (可以做)          不紧急不重要 (后续做)
┌────────────────────────┐  ┌────────────────────────┐
│ P2 WebSocket 连接限制     │  │ P3 K8s 部署              │
│ P2 连接池 / 缓存          │  │ P3 Prometheus 指标       │
│ P2 会议列表分页            │  │ P3 文件压缩 / 过期清理    │
│                          │  │ P3 负载测试              │
└────────────────────────┘  └────────────────────────┘
```

---

## Phase 1: 安全与数据完整性 (P0 — 必须做)

### 1.1 API 认证鉴权

**现状**: 所有 HTTP/WebSocket 端点完全开放。

**方案**: API Key + JWT Token 双层认证

```
客户端                          服务器
  │                               │
  │── POST /api/auth/token ──────>│  (API Key in header)
  │                               │  验证 API Key
  │<── { "access_token": "..." }──│  签发 JWT (含 exp, sub, scope)
  │                               │
  │── GET /api/meetings ─────────>│  (Authorization: Bearer <JWT>)
  │                               │  验证 JWT 有效期
  │<── { "meetings": [...] }──────│
  │                               │
  │── WS /ws/meeting ───────────>│  (query: token=<JWT>)
  │                               │  握手时验证 JWT
  │                               │
```

**新增文件**: `auth.py`

```python
"""Authentication middleware — API Key issuance + JWT verification."""

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import jwt  # PyJWT
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

API_KEY_HEADER = "X-API-Key"


@dataclass
class AuthConfig:
    api_key: str                           # 管理员 API Key (用于签发 Token)
    jwt_secret: str                        # JWT 签名密钥
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: float = 24.0         # Token 有效期
    api_key_hash: str = ""                 # 哈希存储的 API Key

    def __post_init__(self):
        # 只存储哈希，避免明文泄露
        self.api_key_hash = hashlib.sha256(self.api_key.encode()).hexdigest()


def create_token(subject: str, config: AuthConfig) -> str:
    payload = {
        "sub": subject,
        "iat": int(time.time()),
        "exp": int(time.time() + config.jwt_expire_hours * 3600),
    }
    return jwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm)


def verify_token(token: str, config: AuthConfig) -> dict:
    try:
        return jwt.decode(token, config.jwt_secret, algorithms=[config.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def verify_api_key(request: Request, config: AuthConfig) -> bool:
    """验证请求头中的 API Key (用于 /api/auth/token 端点)"""
    key = request.headers.get(API_KEY_HEADER, "")
    if hashlib.sha256(key.encode()).hexdigest() != config.api_key_hash:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


# FastAPI Dependencies
bearer_scheme = HTTPBearer()

async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    config: AuthConfig = Depends(get_auth_config),  # 从 app state 获取
) -> dict:
    return verify_token(credentials.credentials, config)


async def require_ws_auth(websocket: WebSocket, config: AuthConfig) -> dict:
    """WebSocket 连接时验证 Token"""
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        raise ValueError("Missing token")
    try:
        return verify_token(token, config)
    except HTTPException:
        await websocket.close(code=4001, reason="Invalid token")
        raise
```

**修改**: 所有 `@app.get("/api/...")` 和 `@app.post("/api/...")` 添加 `Depends(require_auth)`:

```python
# http_endpoints.py
@app.get("/api/meetings")
async def list_meetings(auth: dict = Depends(require_auth)):  # ← 添加 dependency
    ...
```

**WebSocket 验证**: 在 `ws_handler.py` 的 `websocket_meeting()` 中 `await websocket.accept()` 之后立即验证。

**新增端点**:

```python
@app.post("/api/auth/token")
async def get_token(request: Request, config: AuthConfig = Depends(get_auth_config)):
    """用 API Key 换取 JWT"""
    verify_api_key(request, config)
    token = create_token(subject="api-client", config=config)
    return {"access_token": token, "token_type": "bearer", "expires_in": int(config.jwt_expire_hours * 3600)}
```

**环境变量**:

```bash
# .env — 生产环境
AUTH_API_KEY=<随机生成的 64 字符 hex>       # openssl rand -hex 32
AUTH_JWT_SECRET=<随机生成的 64 字符 hex>    # openssl rand -hex 32
AUTH_JWT_EXPIRE_HOURS=24
```

### 1.2 .env 安全 + 密钥管理

**现状**: `.env` 文件在 git 中（含 `OPENAI_API_KEY` 等字段）。

**措施**:

1. **`.gitignore` 增强**:

```gitignore
# .gitignore
.env
.env.local
.env.production
*.sqlite3
recordings/
__pycache__/
*.pyc
```

2. **创建 `.env.example`** (不含任何真实值):

```bash
# .env.example — 所有配置项的模板，提交到 git
# 复制为 .env 后填入实际值
# cp .env.example .env

# ── 认证 ────────────────────────────────────
AUTH_API_KEY=changeme-64-char-hex
AUTH_JWT_SECRET=changeme-64-char-hex
AUTH_JWT_EXPIRE_HOURS=24

# ── LLM ─────────────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b

# ── ASR ─────────────────────────────────────
ASR_BACKEND=mlx
ASR_DEVICE=cpu
ASR_LANGUAGE=auto
FINAL_ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-1.7B
FINAL_ASR_ALIGNER_PATH=~/whisper-models/Qwen3-ForcedAligner-0.6B

# ── Server ──────────────────────────────────
HOST=0.0.0.0
PORT=8800
```

3. **Docker Secrets** (替代直接 COPY .env):

```yaml
# docker-compose.yml
services:
  app:
    secrets:
      - auth_api_key
      - auth_jwt_secret
    environment:
      AUTH_API_KEY_FILE: /run/secrets/auth_api_key
      AUTH_JWT_SECRET_FILE: /run/secrets/auth_jwt_secret

secrets:
  auth_api_key:
    file: ./secrets/auth_api_key.txt
  auth_jwt_secret:
    file: ./secrets/auth_jwt_secret.txt
```

4. **config.py 增加 Docker secrets 支持**:

```python
def _secret_from_file(env_var: str, fallback_var: str, default: str = "") -> str:
    """优先从 _FILE 后缀的环境变量读取文件内容 (Docker secrets)"""
    file_path = os.getenv(env_var + "_FILE", "")
    if file_path:
        try:
            return Path(file_path).read_text().strip()
        except OSError:
            pass
    return os.getenv(fallback_var, default)
```

### 1.3 SQLite 生产化

**现状**:
```python
# persistence.py:24-28 — 每次操作新建连接，无 WAL，无 PRAGMA 优化
def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(str(self.db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

**改造后**:

```python
"""persistence.py — 生产级 SQLite 持久层"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class MeetingStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(os.path.expanduser(str(db_path)))
        if not self.db_path.is_absolute():
            self.db_path = Path.cwd() / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # ── 读写锁 ─────────────────────────────────────
        self._write_lock = threading.Lock()

        # ── 连接池 (per-thread, 写操作用锁序列化) ─────
        self._local = threading.local()

        # ── 初始化 ─────────────────────────────────────
        self._initialize_schema()
        self._run_migrations()

    def _get_connection(self) -> sqlite3.Connection:
        """每个线程持有一个连接 (SQLite 要求连接不跨线程)"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")           # ← 关键: WAL 模式
            conn.execute("PRAGMA synchronous = NORMAL")          # ← 性能优化: 允许少量丢失
            conn.execute("PRAGMA foreign_keys = ON")             # ← 外键约束
            conn.execute("PRAGMA cache_size = -64000")           # ← 64MB 页缓存
            conn.execute("PRAGMA busy_timeout = 5000")           # ← 5s 锁等待
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")     # ← 渐进式回收
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _read_connection(self):
        """只读操作的连接上下文 (WAL 模式下可并发读)"""
        conn = self._get_connection()
        try:
            yield conn
        finally:
            pass  # 连接复用，不关闭

    @contextmanager
    def _write_connection(self):
        """写操作的连接上下文 (用锁序列化)"""
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
            finally:
                pass

    def _initialize_schema(self) -> None:
        with self._write_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meetings (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    transcript_count INTEGER NOT NULL DEFAULT 0,
                    chat_count INTEGER NOT NULL DEFAULT 0,
                    summary_text TEXT,
                    summary_updated_at TEXT,
                    summary_turn_count INTEGER NOT NULL DEFAULT 0,
                    recording_path TEXT,
                    recording_bytes INTEGER NOT NULL DEFAULT 0,
                    recording_error TEXT,
                    source TEXT NOT NULL DEFAULT 'live'
                );
                CREATE INDEX IF NOT EXISTS idx_meetings_created
                    ON meetings(created_at DESC);
                -- ... (transcript_segments, chat_messages 同现有 schema)
            """)

    def _run_migrations(self) -> None:
        """版本化数据库迁移"""
        with self._write_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            applied = {row[0] for row in conn.execute(
                "SELECT version FROM _migrations"
            ).fetchall()}

            if 1 not in applied:
                conn.execute("UPDATE meetings SET source = 'live' WHERE source IS NULL")
                conn.execute(
                    "INSERT INTO _migrations VALUES (1, ?)",
                    (datetime.now().isoformat(),),
                )
            # 未来新版本迁移:
            # if 2 not in applied:
            #     conn.execute("ALTER TABLE ... ADD COLUMN ...")
            #     conn.execute("INSERT INTO _migrations VALUES (2, ?)", ...)

    def list_meetings(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._read_connection() as conn:          # ← 只读
            rows = conn.execute(
                """
                SELECT ...
                FROM meetings
                WHERE transcript_count > 0
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()
        return [self._meeting_summary_from_row(row) for row in rows]

    def insert_transcript_segment(self, ...) -> None:
        with self._write_connection() as conn:          # ← 写入，受锁保护
            ...  # 同现有逻辑

    def backup(self, backup_path: str | Path) -> None:
        """在线备份 (SQLite backup API, 不阻塞读操作)"""
        with self._read_connection() as conn:
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()

    def close(self) -> None:
        """关闭线程本地连接"""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
```

**关键改动**:

| 改动 | 原始 | 改后 | 影响 |
|------|------|------|------|
| Journal 模式 | DELETE | **WAL** | 读写可并发，多会话不再阻塞 |
| Cache 大小 | 默认 2MB | **64MB** | 查询速度 5-10x |
| 锁等待 | 默认 5s | **5s (明确设置)** | 防止 busy timeout 未定义 |
| 同步模式 | FULL | **NORMAL** | 写性能 2x (极少数据丢失风险) |
| 连接复用 | 每次新建 | **per-thread 复用** | 消除连接开销 |
| 写锁 | 无 | **threading.Lock** | 防止并发写入损坏 |
| 自动清理 | 无 | **AUTO_VACUUM = INCREMENTAL** | 空间自动回收 |
| 迁移机制 | _ensure_column | **版本化 _migrations** | 可追踪、可回滚 |

### 1.4 输入消毒与注入防护

**现状**: 转写文本、聊天消息、session_id 等均未消毒。

**措施**:

```python
"""validators.py — 新增"""

import re
import html

# session_id 格式: 8 字符 hex
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8,16}$")

# segment_id 格式: UUID
SEGMENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def sanitize_text(text: str, max_length: int = 50000) -> str:
    """消毒用户输入文本，防止 XSS 和注入"""
    if not text:
        return ""
    text = text[:max_length]
    # HTML 实体编码 (防止存储后在前端渲染 XSS)
    text = html.escape(text)
    # 移除控制字符 (保留换行和制表符)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()


def validate_session_id(session_id: str) -> str:
    """验证 session_id 格式，防止路径遍历"""
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")
    return session_id


def validate_segment_id(segment_id: str) -> str:
    if not SEGMENT_ID_RE.match(segment_id):
        raise HTTPException(status_code=400, detail="Invalid segment_id format")
    return segment_id
```

**应用**: 在每个 HTTP 端点的路径参数和请求体中使用:

```python
# http_endpoints.py
@app.get("/api/meetings/{session_id}")
async def get_meeting(session_id: str = Path(...)):
    session_id = validate_session_id(session_id)  # ← 格式验证
    meeting = meeting_store.get_meeting(session_id)

@app.patch("/api/meetings/{session_id}/transcript/{segment_id}")
async def update_segment(session_id: str, segment_id: str, body: dict):
    session_id = validate_session_id(session_id)
    segment_id = validate_segment_id(segment_id)
    text = sanitize_text(body.get("text", ""))  # ← 文本消毒
    speaker = sanitize_text(body.get("speaker", ""), max_length=200)
```

### 1.5 Session 并发安全

**现状**: `session_manager.sessions` dict 在异步上下文中无锁。

**改造**:

```python
# session.py
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, MeetingSession] = {}
        self._lock = asyncio.Lock()
        self._max_sessions = 50  # ← 新增: 最大并发会话数

    async def create_session(self) -> MeetingSession:
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("Max concurrent sessions reached")
            session = MeetingSession(...)
            self._sessions[session.session_id] = session
            return session

    async def get_session(self, session_id: str) -> MeetingSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def list_active(self) -> list[MeetingSession]:
        async with self._lock:
            return list(self._sessions.values())
```

---

## Phase 2: 可靠性与稳定性 (P1 — 强烈建议)

### 2.1 统一错误处理

**新增全局异常处理器**:

```python
# server.py 或新建 middleware.py
import logging
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("meeting.errors")

def install_error_handlers(app: FastAPI):
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception: %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": "server_error"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        logger.warning("Validation error: %s", exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Validation error",
                "type": "validation_error",
                "errors": exc.errors(),
            },
        )
```

**统一响应信封** (可选但推荐):

```python
# 所有成功响应统一格式:
{
    "success": true,
    "data": { ... },       # 实际数据
    "meta": {              # 可选元数据
        "total": 42,
        "limit": 20,
        "offset": 0
    }
}

# 错误响应:
{
    "success": false,
    "error": {
        "type": "validation_error",
        "detail": "text is required",
        "field": "text"
    }
}
```

### 2.2 数据库定时备份

**新增**: `backup_scheduler.py`

```python
"""定时数据库备份 (每天凌晨 3 点)"""

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("meeting.backup")


class BackupScheduler:
    def __init__(self, meeting_store, backup_dir: str = "backups", keep_days: int = 30):
        self.store = meeting_store
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._loop(), name="backup-scheduler")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while True:
            now = datetime.now()
            # 计算到下一个 03:00 的秒数
            next_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_3am <= now:
                next_3am = next_3am.replace(day=next_3am.day + 1)
            wait_seconds = (next_3am - now).total_seconds()

            await asyncio.sleep(wait_seconds)
            try:
                self._do_backup()
                self._cleanup_old_backups()
            except Exception:
                logger.error("Backup failed", exc_info=True)

    def _do_backup(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"meeting_{ts}.sqlite3"
        self.store.backup(backup_path)

        # 压缩
        import gzip
        gz_path = backup_path.with_suffix(".sqlite3.gz")
        with open(backup_path, "rb") as f_in:
            with gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        backup_path.unlink()

        logger.info("Backup created: %s (%.1f MB)", gz_path.name, gz_path.stat().st_size / 1e6)

    def _cleanup_old_backups(self):
        import time
        cutoff = time.time() - self.keep_days * 86400
        for f in self.backup_dir.glob("meeting_*.sqlite3.gz"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info("Cleaned old backup: %s", f.name)
```

**server.py 启动时**:

```python
from backup_scheduler import BackupScheduler
backup = BackupScheduler(meeting_store, backup_dir="backups", keep_days=30)
# 在 startup event 中:
await backup.start()
```

### 2.3 数据库定时清理

```python
# 在 BackupScheduler 或独立的 CleanupScheduler 中:
def cleanup_expired_meetings(self, max_age_days: int = 90):
    """删除超过 max_age_days 的会议及其录音"""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with self.store._write_connection() as conn:
        old_ids = conn.execute(
            "SELECT session_id, recording_path FROM meetings WHERE ended_at < ?",
            (cutoff,),
        ).fetchall()
        for row in old_ids:
            if row["recording_path"]:
                path = Path(row["recording_path"])
                if path.exists():
                    path.unlink()
            conn.execute("DELETE FROM meetings WHERE session_id = ?", (row["session_id"],))
        logger.info("Cleaned %d expired meetings (older than %d days)", len(old_ids), max_age_days)
```

### 2.4 API 版本化

**修改前缀**: `/api/...` → `/api/v1/...`

```python
# http_endpoints.py
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")  # ← 版本化

@router.get("/health")
@router.get("/readiness")
@router.get("/meetings")
@router.get("/meetings/{session_id}")
# ... 所有端点

# server.py
app.include_router(router)
```

**保留旧路由重定向** (平滑迁移):

```python
# 向后兼容: /api/xxx 重定向到 /api/v1/xxx
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def legacy_api_redirect(request: Request, path: str):
    from starlette.responses import RedirectResponse
    new_url = str(request.url).replace("/api/", "/api/v1/", 1)
    return RedirectResponse(url=new_url, status_code=308)
```

### 2.5 WebSocket 生产化

```python
# ws_handler.py
class ConnectionManager:
    def __init__(self, max_connections: int = 20):
        self.max_connections = max_connections
        self._active: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def accept(self, websocket: WebSocket, session_id: str) -> bool:
        async with self._lock:
            if len(self._active) >= self.max_connections:
                await websocket.close(code=4003, reason="Max connections reached")
                return False
            self._active[session_id] = websocket
            return True

    async def remove(self, session_id: str):
        async with self._lock:
            self._active.pop(session_id, None)


# 服务端主动心跳
async def _heartbeat(websocket: WebSocket, interval: int = 30):
    """每 30 秒发送 ping，检测断线"""
    try:
        while True:
            await asyncio.sleep(interval)
            await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass


# 在 websocket_meeting() 中:
# 1. 连接限流
if not await connection_manager.accept(websocket, session_id):
    return

# 2. 启动心跳任务
heartbeat_task = asyncio.create_task(_heartbeat(websocket))

# 3. 断开时清理
finally:
    heartbeat_task.cancel()
    await connection_manager.remove(session_id)
```

### 2.6 文件清理

```python
# 在 cleanup scheduler 中:
def cleanup_orphaned_recordings(self):
    """删除数据库中无引用的录音文件"""
    recordings_dir = Path(self.store.db_path.parent / "recordings")
    if not recordings_dir.exists():
        return

    with self.store._read_connection() as conn:
        referenced = {
            row[0] for row in conn.execute(
                "SELECT recording_path FROM meetings WHERE recording_path IS NOT NULL"
            ).fetchall()
        }

    for f in recordings_dir.glob("*.wav"):
        if str(f) not in referenced:
            age_days = (time.time() - f.stat().st_mtime) / 86400
            if age_days > 7:  # 7 天后清理
                f.unlink()
                logger.info("Cleaned orphaned recording: %s", f.name)
```

---

## Phase 3: 可观测性 (P2 — 生产运维必备)

### 3.1 结构化日志 (JSON)

```python
# logging_config.py — 替换现有 logging 配置
import logging
import json
import sys
from datetime import datetime


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # 添加 trace_id (如果有)
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(level: str = "INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    root.handlers.clear()
    root.addHandler(handler)

    # 静音第三方库
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
```

### 3.2 请求追踪中间件

```python
# middleware.py
import uuid
from starlette.middleware.base import BaseHTTPMiddleware


class RequestTracingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-ID", str(uuid.uuid4())[:8])
        request.state.trace_id = trace_id

        # 注入到所有日志中
        import logging
        old_factory = logging.getLogRecordFactory()
        def factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            record.trace_id = trace_id
            return record
        logging.setLogRecordFactory(factory)

        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response

app.add_middleware(RequestTracingMiddleware)
```

### 3.3 Prometheus 指标 (可选)

```python
# metrics.py — 轻量级指标收集
from dataclasses import dataclass, field
import time


@dataclass
class Metrics:
    ws_connections_total: int = 0
    ws_connections_active: int = 0
    http_requests_total: int = 0
    http_request_duration_sum: float = 0.0
    http_request_errors: int = 0
    upload_total: int = 0
    upload_bytes_total: int = 0
    transcription_seconds_total: float = 0.0
    asr_queue_depth: int = 0
    llm_calls_total: int = 0
    llm_call_errors: int = 0

    # Prometheus 格式输出
    def to_prometheus(self) -> str:
        lines = []
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            metric_name = f"meetscribe_{field_name}"
            lines.append(f"# TYPE {metric_name} gauge")
            lines.append(f"{metric_name} {value}")
        return "\n".join(lines)

metrics = Metrics()

# http_endpoints.py 添加:
@app.get("/api/v1/metrics")
async def prometheus_metrics():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(metrics.to_prometheus())
```

### 3.4 日志关键事件

```python
# 应在以下位置添加结构化日志:

# 1. WebSocket 连接生命周期
logger.info("ws_connect", extra={
    "session_id": session.session_id,
    "client_ip": websocket.client.host,
    "user_agent": websocket.headers.get("user-agent", ""),
})

# 2. ASR 推理性能
logger.info("asr_complete", extra={
    "session_id": session_id,
    "duration_sec": audio_duration,
    "processing_sec": processing_time,
    "rtf": processing_time / audio_duration,
    "backend": "mlx",
    "chars": len(text),
})

# 3. 上传处理
logger.info("upload_complete", extra={
    "filename": filename,
    "size_mb": file_size / 1e6,
    "duration_sec": audio_duration,
    "processing_sec": processing_time,
    "segments": len(segments),
})

# 4. LLM 调用
logger.info("llm_call", extra={
    "model": llm_config.model_name,
    "provider": llm_config.provider,
    "duration_sec": elapsed,
    "tokens": token_count,
})

# 5. 错误事件
logger.error("asr_failed", extra={
    "session_id": session_id,
    "error_type": type(exc).__name__,
    "error_msg": str(exc),
}, exc_info=exc)
```

---

## Phase 4: 性能与扩展性 (P3 — 按需)

### 4.1 Redis 会话缓存 (多实例部署时)

```
当前: 内存 dict (session_manager._sessions)
      → 单机部署，进程重启丢失

改造: Redis (pub/sub 同步多实例)

┌──────────┐     ┌──────────┐     ┌──────────┐
│ App 实例1 │     │   Redis  │     │ App 实例2 │
│ sessions  │────>│ pub/sub  │<────│ sessions  │
│ 内存缓存  │     │ 持久化    │     │ 内存缓存  │
└──────────┘     └──────────┘     └──────────┘
```

**注意**: 当前是单机 Apple Silicon MLX 部署，Redis 通常不需要。只有水平扩展到多实例时才需要。

### 4.2 LLM 连接池 + 熔断器

```python
# circuit_breaker.py
class CircuitBreaker:
    """防止 LLM 服务故障时雪崩"""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._last_failure = 0.0
        self._state = "closed"  # closed / open / half_open

    async def call(self, func, *args, **kwargs):
        if self._state == "open":
            if time.time() - self._last_failure > self.reset_timeout:
                self._state = "half_open"
            else:
                raise RuntimeError("Circuit breaker open — LLM unavailable")

        try:
            result = await func(*args, **kwargs)
            self._failures = 0
            self._state = "closed"
            return result
        except Exception:
            self._failures += 1
            self._last_failure = time.time()
            if self._failures >= self.failure_threshold:
                self._state = "open"
                logger.error("Circuit breaker OPEN after %d failures", self._failures)
            raise
```

### 4.3 录音压缩

```python
# 实时录制时直接写 FLAC (而非 WAV):
# 16kHz 16-bit mono WAV: 32KB/s → 1 小时 = 115MB
# 16kHz FLAC: ~8-16KB/s → 1 小时 = 30-60MB

# 可在 audio_recorder 中集成 ffmpeg 实时转码:
# import subprocess
# process = subprocess.Popen(
#     ["ffmpeg", "-f", "s16le", "-ar", "16000", "-ac", "1",
#      "-i", "-", "-c:a", "flac", output_path],
#     stdin=subprocess.PIPE,
# )
# process.stdin.write(pcm_data)
```

---

## Phase 5: 运维自动化 (P3 — 按需)

### 5.1 Nginx 反向代理 + TLS

```nginx
# /etc/nginx/conf.d/meetscribe.conf
server {
    listen 443 ssl http2;
    server_name meetscribe.example.com;

    ssl_certificate     /etc/ssl/certs/meetscribe.crt;
    ssl_certificate_key /etc/ssl/private/meetscribe.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # HTTP API
    location /api/ {
        proxy_pass http://127.0.0.1:8800;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600;  # 10 分钟 (上传大文件)
        client_max_body_size 512M;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8800;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;  # 24 小时 (长时间会议)
        proxy_send_timeout 86400;
    }

    # 前端 SPA
    location / {
        proxy_pass http://127.0.0.1:8800;
    }
}

server {
    listen 80;
    server_name meetscribe.example.com;
    return 301 https://$server_name$request_uri;
}
```

### 5.2 Systemd Service (裸机部署)

```ini
# /etc/systemd/system/meetscribe.service
[Unit]
Description=MeetScribe Realtime Voice
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=meetscribe
Group=meetscribe
WorkingDirectory=/opt/meetscribe
ExecStart=/opt/meetscribe/.venv/bin/python -m uvicorn server:app \
    --host 0.0.0.0 --port 8800 --workers 1 \
    --log-level info --access-log
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

# 安全加固
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/meetscribe/data /opt/meetscribe/recordings /opt/meetscribe/backups
PrivateTmp=true

# 资源限制
LimitNOFILE=65536
MemoryMax=8G

# 环境
EnvironmentFile=/opt/meetscribe/.env

[Install]
WantedBy=multi-user.target
```

### 5.3 Docker 生产配置

```yaml
# docker-compose.prod.yml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: mrv-app
    restart: always
    environment:
      HOST: "0.0.0.0"
      PORT: "8800"
      OLLAMA_BASE_URL: "http://ollama:11434"
      ASR_MODEL_PATH: "/models/Qwen3-ASR-1.7B"
      ASR_BACKEND: "mlx"
      # 认证
      AUTH_API_KEY_FILE: "/run/secrets/auth_api_key"
      AUTH_JWT_SECRET_FILE: "/run/secrets/auth_jwt_secret"
    volumes:
      - models:/models:ro
      - data:/app/data
      - recordings:/app/recordings
      - backups:/app/backups
    deploy:
      resources:
        limits:
          memory: 6G       # MLX 1.7B-4bit 约 2GB + 上下文
        reservations:
          memory: 4G
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "10"
    secrets:
      - auth_api_key
      - auth_jwt_secret

secrets:
  auth_api_key:
    file: ./secrets/auth_api_key.txt
  auth_jwt_secret:
    file: ./secrets/auth_jwt_secret.txt
```

---

## 6. 改造总览时间线

```
Week 1-2: Phase 1 — 安全 + 数据完整性
├── Day 1-2:   auth.py (API Key + JWT 认证)
├── Day 3:     .env 安全 + 密钥管理
├── Day 4-5:   persistence.py (WAL + 连接池 + 迁移)
├── Day 6:     validators.py (输入消毒)
├── Day 7:     session 并发安全 (asyncio.Lock)
└── Day 8-10:  集成测试 + 回归验证

Week 3: Phase 2 — 可靠性
├── Day 1:     全局异常处理器 + 统一响应格式
├── Day 2:     数据库定时备份
├── Day 3:     API v1 版本化
├── Day 4:     WebSocket 连接限制 + 心跳
└── Day 5:     文件清理任务

Week 4: Phase 3 — 可观测性
├── Day 1-2:   结构化 JSON 日志
├── Day 3:     请求追踪中间件 (trace_id)
├── Day 4-5:   关键事件日志审计 + 压力测试

Week 5+: Phase 4-5 — 按需
├── LLM 熔断器
├── Nginx 反向代理
├── Systemd service
├── Docker 生产配置
└── Prometheus 指标
```

---

## 附录: 改造前后对比

| 维度 | 改造前 | 改造后 |
|------|--------|--------|
| 认证 | 无 | API Key + JWT |
| 数据库模式 | DELETE journal | WAL (读写并发) |
| 数据库连接 | 每次新建 | per-thread 复用 |
| 数据库备份 | 无 | 每日自动 gzip 备份 |
| 数据库迁移 | _ensure_column 硬编码 | 版本化 _migrations |
| 密钥管理 | .env 在 git 中 | Docker secrets + 文件注入 |
| 输入消毒 | 无 | html.escape + 正则验证 |
| Session 并发 | 无锁 dict | asyncio.Lock + 最大数限制 |
| 错误处理 | 吞异常 | 全局 handler + 结构化响应 |
| 日志格式 | 纯文本 | JSON (trace_id, context) |
| API 路径 | /api/xxx | /api/v1/xxx (版本化) |
| WebSocket | 无限连接 | 最大连接数 + 心跳 + 背压 |
| 文件清理 | 无 | 7 天孤儿清理 + 90 天过期 |
| 部署 | 开发级 docker-compose | Nginx + TLS + Systemd |
