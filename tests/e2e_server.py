import argparse
import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import server
from persistence import MeetingStore


class FakeASRService:
    def __init__(self):
        self._initialized = True

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def wait_ready(self, timeout=None):
        return True

    def initialization_state(self):
        return "ready"

    def initialization_error(self):
        return None


def seed_meeting(store: MeetingStore, recording_path: Path, session_id: str = "e2e-meeting-001"):
    created_at = "2026-04-07T18:00:00"
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    recording_path.write_bytes(b"RIFFe2e-demo-audio")

    store.insert_transcript_segment(
        session_id,
        created_at,
        SimpleNamespace(
            id="seg-001",
            speaker="发言人",
            text="我们决定在周五上线，并在周四完成回归。",
            start_time=0.0,
            end_time=4.2,
            timestamp="2026-04-07T18:00:02",
        ),
        ordinal=1,
    )
    store.insert_chat_message(
        session_id,
        created_at,
        SimpleNamespace(
            id="chat-001",
            role="assistant",
            content="当前会议结论是周五上线，周四回归。",
            timestamp="2026-04-07T18:00:08",
        ),
        ordinal=1,
    )
    store.upsert_summary(
        session_id,
        created_at,
        "会议确定周五上线，并要求周四完成回归。",
        updated_at="2026-04-07T18:00:20",
        turn_count=1,
    )
    store.complete_session(
        session_id,
        created_at,
        ended_at="2026-04-07T18:30:00",
        recording_path=str(recording_path),
        recording_bytes=recording_path.stat().st_size,
        recording_error=None,
        status="completed",
    )
    return session_id


async def fake_regenerate_meeting_summary(session_id: str):
    meeting = server.get_meeting_or_404(session_id)
    updated_summary = "重算摘要：周五上线，周四完成回归，主持人负责最终确认。"
    server.meeting_store.upsert_summary(
        session_id,
        meeting.get("created_at") or "2026-04-07T18:00:00",
        updated_summary,
        updated_at="2026-04-07T19:00:00",
        turn_count=meeting.get("transcript_count") or len(meeting.get("transcript", [])),
    )
    return server.get_meeting_or_404(session_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("E2E_PORT", "8810")))
    args = parser.parse_args()

    tempdir = TemporaryDirectory(prefix="meeting-realtime-e2e-")
    root = Path(tempdir.name)
    store = MeetingStore(root / "meetings.sqlite3")
    recording_path = root / "recordings" / "e2e-meeting-001.wav"
    seed_meeting(store, recording_path)

    server.asr_service = FakeASRService()
    server.meeting_store = store
    server.session_manager.store = store
    server.regenerate_meeting_summary = fake_regenerate_meeting_summary

    config = uvicorn.Config(
        server.app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    app_server = uvicorn.Server(config)

    try:
        asyncio.run(app_server.serve())
    finally:
        tempdir.cleanup()


if __name__ == "__main__":
    main()
