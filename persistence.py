"""
SQLite persistence for meeting history, transcript, summary, chat, and recordings.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class MeetingStore:
    """Persist completed and in-progress meetings into a local SQLite database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(os.path.expanduser(str(db_path)))
        if not self.db_path.is_absolute():
            self.db_path = Path.cwd() / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
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
                    recording_error TEXT
                );

                CREATE TABLE IF NOT EXISTS transcript_segments (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    capture_start_time REAL NOT NULL DEFAULT 0,
                    capture_duration REAL NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES meetings(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_transcript_session_ordinal
                    ON transcript_segments(session_id, ordinal);

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES meetings(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_chat_session_ordinal
                    ON chat_messages(session_id, ordinal);

                CREATE TABLE IF NOT EXISTS templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    system_prompt TEXT NOT NULL,
                    user_prompt TEXT NOT NULL,
                    output_format TEXT NOT NULL DEFAULT 'markdown',
                    language TEXT NOT NULL DEFAULT '',
                    is_preset INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "meetings", "source", "TEXT NOT NULL DEFAULT 'live'")
            self._ensure_column(conn, "meetings", "template_id", "TEXT")
            self._ensure_column(conn, "meetings", "last_summarized_ordinal", "INTEGER NOT NULL DEFAULT 0")
            added_capture_start = self._ensure_column(
                conn,
                "transcript_segments",
                "capture_start_time",
                "REAL NOT NULL DEFAULT 0",
            )
            added_capture_duration = self._ensure_column(
                conn,
                "transcript_segments",
                "capture_duration",
                "REAL NOT NULL DEFAULT 0",
            )
            if added_capture_start:
                conn.execute("UPDATE transcript_segments SET capture_start_time = start_time")
            if added_capture_duration:
                conn.execute(
                    "UPDATE transcript_segments SET capture_duration = max(end_time - start_time, 0.0)"
                )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing:
            return False
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True

    def _ensure_meeting(self, conn: sqlite3.Connection, session_id: str, created_at: str) -> None:
        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO meetings (
                session_id, created_at, updated_at, status
            ) VALUES (?, ?, ?, 'active')
            """,
            (session_id, created_at, now),
        )

    def insert_transcript_segment(
        self,
        session_id: str,
        created_at: str,
        segment: Any,
        ordinal: int,
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                INSERT OR REPLACE INTO transcript_segments (
                    id, session_id, ordinal, speaker, text, start_time, end_time,
                    capture_start_time, capture_duration, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment.id,
                    session_id,
                    ordinal,
                    segment.speaker,
                    segment.text,
                    float(segment.start_time),
                    float(segment.end_time),
                    float(getattr(segment, "capture_start_time", segment.start_time)),
                    float(getattr(segment, "capture_duration", max(segment.end_time - segment.start_time, 0.0))),
                    segment.timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE meetings
                SET transcript_count = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (ordinal, datetime.now().isoformat(), session_id),
            )

    def insert_chat_message(
        self,
        session_id: str,
        created_at: str,
        message: Any,
        ordinal: int,
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                INSERT OR REPLACE INTO chat_messages (
                    id, session_id, ordinal, role, content, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    session_id,
                    ordinal,
                    message.role,
                    message.content,
                    message.timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE meetings
                SET chat_count = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (ordinal, datetime.now().isoformat(), session_id),
            )

    def upsert_summary(
        self,
        session_id: str,
        created_at: str,
        text: str,
        updated_at: str,
        turn_count: int,
        template_id: str | None = None,
        last_summarized_ordinal: int = 0,
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                UPDATE meetings
                SET summary_text = ?, summary_updated_at = ?, summary_turn_count = ?,
                    updated_at = ?, template_id = ?, last_summarized_ordinal = ?
                WHERE session_id = ?
                """,
                (text, updated_at, turn_count, updated_at, template_id, last_summarized_ordinal, session_id),
            )

    def complete_session(
        self,
        session_id: str,
        created_at: str,
        ended_at: str,
        *,
        recording_path: str | None,
        recording_bytes: int,
        recording_error: str | None,
        status: str = "completed",
        source: str = "live",
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                UPDATE meetings
                SET ended_at = ?, updated_at = ?, status = ?, recording_path = ?, recording_bytes = ?, recording_error = ?, source = ?
                WHERE session_id = ?
                """,
                (
                    ended_at,
                    ended_at,
                    status,
                    recording_path,
                    int(recording_bytes or 0),
                    recording_error,
                    source,
                    session_id,
                ),
            )

    def list_meetings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, created_at, updated_at, ended_at, status,
                       transcript_count, chat_count, summary_text, summary_updated_at,
                       summary_turn_count, recording_path, recording_bytes, recording_error,
                       source, last_summarized_ordinal
                FROM meetings
                WHERE transcript_count > 0
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._meeting_summary_from_row(row) for row in rows]

    def get_meeting(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            meeting_row = conn.execute(
                """
                SELECT session_id, created_at, updated_at, ended_at, status,
                       transcript_count, chat_count, summary_text, summary_updated_at,
                       summary_turn_count, recording_path, recording_bytes, recording_error,
                       source, last_summarized_ordinal
                FROM meetings
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if meeting_row is None:
                return None

            transcript_rows = conn.execute(
                """
                SELECT id, ordinal, speaker, text, start_time, end_time,
                       capture_start_time, capture_duration, timestamp
                FROM transcript_segments
                WHERE session_id = ?
                ORDER BY ordinal ASC
                """,
                (session_id,),
            ).fetchall()
            chat_rows = conn.execute(
                """
                SELECT id, ordinal, role, content, timestamp
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY ordinal ASC
                """,
                (session_id,),
            ).fetchall()

        meeting = self._meeting_summary_from_row(meeting_row)
        meeting["transcript"] = [
            {
                "id": row["id"],
                "ordinal": row["ordinal"],
                "speaker": row["speaker"],
                "text": row["text"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "capture_start_time": row["capture_start_time"],
                "capture_duration": row["capture_duration"],
                "timestamp": row["timestamp"],
            }
            for row in transcript_rows
        ]
        meeting["chat_history"] = [
            {
                "id": row["id"],
                "ordinal": row["ordinal"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            for row in chat_rows
        ]
        return meeting

    def update_transcript_segment(
        self,
        session_id: str,
        segment_id: str,
        *,
        text: str,
        speaker: str | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, ordinal, speaker, text, start_time, end_time,
                       capture_start_time, capture_duration, timestamp
                FROM transcript_segments
                WHERE session_id = ? AND id = ?
                """,
                (session_id, segment_id),
            ).fetchone()
            if row is None:
                return None

            new_speaker = speaker.strip() if isinstance(speaker, str) and speaker.strip() else row["speaker"]
            new_text = text.strip()
            conn.execute(
                """
                UPDATE transcript_segments
                SET speaker = ?, text = ?
                WHERE session_id = ? AND id = ?
                """,
                (new_speaker, new_text, session_id, segment_id),
            )
            conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE session_id = ?",
                (datetime.now().isoformat(), session_id),
            )

        return {
            "id": row["id"],
            "ordinal": row["ordinal"],
            "speaker": new_speaker,
            "text": new_text,
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "capture_start_time": row["capture_start_time"],
            "capture_duration": row["capture_duration"],
            "timestamp": row["timestamp"],
        }

    def delete_meeting(self, session_id: str, *, delete_recording: bool = True) -> bool:
        meeting = self.get_meeting(session_id)
        if meeting is None:
            return False

        recording_path = meeting.get("recording_path")
        with self._connect() as conn:
            conn.execute("DELETE FROM meetings WHERE session_id = ?", (session_id,))

        if delete_recording and recording_path:
            path = Path(str(recording_path))
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        return True

    def list_templates(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, description, system_prompt, user_prompt,
                       output_format, language, is_preset, created_at, updated_at
                FROM templates
                ORDER BY is_preset DESC, created_at ASC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "system_prompt": row["system_prompt"],
                "user_prompt": row["user_prompt"],
                "output_format": row["output_format"],
                "language": row["language"],
                "is_preset": bool(row["is_preset"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, description, system_prompt, user_prompt,
                       output_format, language, is_preset, created_at, updated_at
                FROM templates WHERE id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "user_prompt": row["user_prompt"],
            "output_format": row["output_format"],
            "language": row["language"],
            "is_preset": bool(row["is_preset"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_template(
        self,
        template_id: str,
        name: str,
        description: str,
        system_prompt: str,
        user_prompt: str,
        output_format: str = "markdown",
        language: str = "",
        is_preset: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO templates (id, name, description, system_prompt, user_prompt,
                                       output_format, language, is_preset, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (template_id, name, description, system_prompt, user_prompt,
                 output_format, language, 1 if is_preset else 0, now, now),
            )
        return self.get_template(template_id)

    def update_template(self, template_id: str, **fields: Any) -> bool:
        allowed = {"name", "description", "system_prompt", "user_prompt", "output_format", "language"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        now = datetime.now().isoformat()
        values.append(now)
        values.append(template_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE templates SET {set_clause}, updated_at = ? WHERE id = ?",
                values,
            )
        return cursor.rowcount > 0

    def delete_template(self, template_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_preset FROM templates WHERE id = ?", (template_id,)
            ).fetchone()
            if row is None:
                return False
            if row["is_preset"]:
                return False
            conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        return True

    def init_preset_templates(self) -> None:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        if count > 0:
            return

        presets = [
            {
                "id": "preset-standard",
                "name": "標準摘要",
                "description": "通用会议摘要（主题、讨论点、共识、待办）",
                "system_prompt": "You are a meeting note-taker. Generate a concise summary from the meeting transcript, prioritizing decisions, conclusions, and action items.",
                "user_prompt": "Generate a brief summary of the following meeting content, including:\n1. Meeting topic\n2. Main discussion points\n3. Agreements reached\n4. Open or follow-up items\n\nMeeting content:\n{transcript_text}\n\nOutput the summary in concise bullet point format.",
                "output_format": "markdown",
                "language": "Chinese",
            },
            {
                "id": "preset-action-items",
                "name": "行動項提取",
                "description": "只提取 Action Items 和负责人",
                "system_prompt": "You are a meeting assistant focused on extracting action items. Only extract concrete tasks, assignments, and deadlines from the transcript.",
                "user_prompt": "Extract all action items from the following meeting transcript. For each item, identify:\n- The task/action\n- The responsible person (if mentioned)\n- The deadline (if mentioned)\n\nMeeting content:\n{transcript_text}\n\nOutput as a numbered list. If no action items found, say so.",
                "output_format": "markdown",
                "language": "Chinese",
            },
            {
                "id": "preset-formal-minutes",
                "name": "會議紀要",
                "description": "正式会议纪要格式（出席、议题、决议、跟进）",
                "system_prompt": "You are a professional meeting minutes writer. Generate formal meeting minutes in a structured format.",
                "user_prompt": "Generate formal meeting minutes from the following transcript, including:\n1. Meeting topic and date\n2. Attendees/Speakers\n3. Agenda items discussed\n4. Resolutions/Decisions made\n5. Follow-up items and next steps\n\nMeeting content:\n{transcript_text}\n\nOutput in formal meeting minutes format.",
                "output_format": "markdown",
                "language": "Chinese",
            },
            {
                "id": "preset-english",
                "name": "English Summary",
                "description": "English meeting summary with key points and action items",
                "system_prompt": "You are a meeting note-taker. Generate a concise English summary from the meeting transcript, prioritizing decisions, conclusions, and action items.",
                "user_prompt": "Generate a brief English summary of the following meeting content, including:\n1. Meeting topic\n2. Main discussion points\n3. Agreements reached\n4. Open or follow-up items\n\nMeeting content:\n{transcript_text}\n\nOutput the summary in concise bullet point format in English.",
                "output_format": "markdown",
                "language": "English",
            },
        ]

        for p in presets:
            self.create_template(
                template_id=p["id"],
                name=p["name"],
                description=p["description"],
                system_prompt=p["system_prompt"],
                user_prompt=p["user_prompt"],
                output_format=p["output_format"],
                language=p["language"],
                is_preset=True,
            )

    @staticmethod
    def _meeting_summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
        summary_text = row["summary_text"]
        session_id = row["session_id"]
        created_at = row["created_at"] or ""
        # Generate a human-readable title from the timestamp
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(created_at)
            title = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            title = session_id
        return {
            "id": session_id,
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "updated_at": row["updated_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "transcript_count": row["transcript_count"],
            "chat_count": row["chat_count"],
            "summary": summary_text,
            "summary_preview": (summary_text or "")[:160],
            "summary_updated_at": row["summary_updated_at"],
            "summary_turn_count": row["summary_turn_count"],
            "recording_path": row["recording_path"],
            "recording_bytes": row["recording_bytes"],
            "recording_error": row["recording_error"],
            "source": row["source"] if "source" in row.keys() else "live",
            "last_summarized_ordinal": row["last_summarized_ordinal"] if "last_summarized_ordinal" in row.keys() else 0,
        }
