"""
In-memory upload task tracker for async file upload transcription.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("meeting.upload_tasks")

TASK_TTL_SECONDS = 3600


@dataclass
class UploadTask:
    task_id: str
    filename: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"  # "queued" | "processing" | "completed" | "failed"
    tmp_path: str | None = None
    session_id: str | None = None
    result: dict | None = None
    error: str | None = None
    progress: float = 0.0
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_id": self.task_id,
            "filename": self.filename,
            "created_at": self.created_at,
            "status": self.status,
            "progress": self.progress,
        }
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data


class UploadTaskTracker:
    def __init__(self) -> None:
        self._tasks: dict[str, UploadTask] = {}

    def create(
        self, task_id: str, filename: str, tmp_path: str | None = None
    ) -> UploadTask:
        task = UploadTask(task_id=task_id, filename=filename, tmp_path=tmp_path)
        self._tasks[task_id] = task
        logger.info("Created upload task %s for file %s", task_id, filename)
        return task

    def get(self, task_id: str) -> UploadTask | None:
        return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs: Any) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        for key, value in kwargs.items():
            if key.startswith("_"):
                continue
            if not hasattr(task, key):
                continue
            setattr(task, key, value)

    def cleanup_older_than(self, max_age_seconds: float = TASK_TTL_SECONDS) -> int:
        now = time.time()
        to_remove: list[str] = []
        for task_id, task in self._tasks.items():
            if task.status in ("completed", "failed"):
                if now - task.created_at > max_age_seconds:
                    to_remove.append(task_id)
        for task_id in to_remove:
            logger.info("Cleaning up expired task %s", task_id)
            del self._tasks[task_id]
        return len(to_remove)

    async def shutdown(self, timeout: float = 30.0) -> None:
        active = [
            t._asyncio_task for t in self._tasks.values() if t._asyncio_task is not None
        ]
        if not active:
            return
        logger.info("Cancelling %d active upload tasks", len(active))
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
