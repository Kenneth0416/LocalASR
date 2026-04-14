import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from persistence import MeetingStore


class MeetingStoreTests(unittest.TestCase):
    def test_store_round_trip_update_and_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "meetings.sqlite3"
            recording_path = Path(tmpdir) / "sample.wav"
            recording_path.write_bytes(b"RIFFdemo")

            store = MeetingStore(db_path)
            session_id = "session-1234"
            created_at = "2026-04-07T10:00:00"

            store.insert_transcript_segment(
                session_id,
                created_at,
                SimpleNamespace(
                    id="seg-1",
                    speaker="发言人 A",
                    text="第一段内容",
                    start_time=0.0,
                    end_time=2.5,
                    timestamp="2026-04-07T10:00:01",
                ),
                ordinal=1,
            )
            store.insert_chat_message(
                session_id,
                created_at,
                SimpleNamespace(
                    id="chat-1",
                    role="user",
                    content="本次会议结论是什么？",
                    timestamp="2026-04-07T10:00:05",
                ),
                ordinal=1,
            )
            store.upsert_summary(
                session_id,
                created_at,
                "会议讨论了预算安排。",
                updated_at="2026-04-07T10:00:10",
                turn_count=1,
            )
            store.complete_session(
                session_id,
                created_at,
                ended_at="2026-04-07T10:30:00",
                recording_path=str(recording_path),
                recording_bytes=recording_path.stat().st_size,
                recording_error=None,
                status="completed",
            )

            meetings = store.list_meetings(limit=10)
            self.assertEqual(len(meetings), 1)
            self.assertEqual(meetings[0]["session_id"], session_id)
            self.assertEqual(meetings[0]["transcript_count"], 1)
            self.assertEqual(meetings[0]["chat_count"], 1)
            self.assertIn("预算安排", meetings[0]["summary_preview"])

            meeting = store.get_meeting(session_id)
            self.assertIsNotNone(meeting)
            self.assertEqual(meeting["status"], "completed")
            self.assertEqual(len(meeting["transcript"]), 1)
            self.assertEqual(len(meeting["chat_history"]), 1)
            self.assertEqual(meeting["recording_path"], str(recording_path))

            updated_segment = store.update_transcript_segment(
                session_id,
                "seg-1",
                text="第一段内容（已修订）",
                speaker="主持人",
            )
            self.assertIsNotNone(updated_segment)
            self.assertEqual(updated_segment["speaker"], "主持人")
            self.assertEqual(updated_segment["text"], "第一段内容（已修订）")

            meeting = store.get_meeting(session_id)
            self.assertEqual(meeting["transcript"][0]["speaker"], "主持人")
            self.assertEqual(meeting["transcript"][0]["text"], "第一段内容（已修订）")

            deleted = store.delete_meeting(session_id, delete_recording=True)
            self.assertTrue(deleted)
            self.assertIsNone(store.get_meeting(session_id))
            self.assertFalse(recording_path.exists())

