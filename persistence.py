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
                """
            )

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
                    id, session_id, ordinal, speaker, text, start_time, end_time, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment.id,
                    session_id,
                    ordinal,
                    segment.speaker,
                    segment.text,
                    float(segment.start_time),
                    float(segment.end_time),
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
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                UPDATE meetings
                SET summary_text = ?, summary_updated_at = ?, summary_turn_count = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (text, updated_at, turn_count, updated_at, session_id),
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
    ) -> None:
        with self._connect() as conn:
            self._ensure_meeting(conn, session_id, created_at)
            conn.execute(
                """
                UPDATE meetings
                SET ended_at = ?, updated_at = ?, status = ?, recording_path = ?, recording_bytes = ?, recording_error = ?
                WHERE session_id = ?
                """,
                (
                    ended_at,
                    ended_at,
                    status,
                    recording_path,
                    int(recording_bytes or 0),
                    recording_error,
                    session_id,
                ),
            )

    def list_meetings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, created_at, updated_at, ended_at, status,
                       transcript_count, chat_count, summary_text, summary_updated_at,
                       summary_turn_count, recording_path, recording_bytes, recording_error
                FROM meetings
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
                       summary_turn_count, recording_path, recording_bytes, recording_error
                FROM meetings
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if meeting_row is None:
                return None

            transcript_rows = conn.execute(
                """
                SELECT id, ordinal, speaker, text, start_time, end_time, timestamp
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
                SELECT id, session_id, ordinal, speaker, text, start_time, end_time, timestamp
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

    @staticmethod
    def _meeting_summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
        summary_text = row["summary_text"]
        return {
            "session_id": row["session_id"],
            "created_at": row["created_at"],
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
        }
