import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from .database import connect


class EventBus:
    def __init__(self) -> None:
        self.subscribers: dict[int, set[asyncio.Queue[str]]] = defaultdict(set)

    @asynccontextmanager
    async def subscribe(self, task_id: int) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self.subscribers[task_id].add(queue)
        try:
            yield queue
        finally:
            self.subscribers[task_id].discard(queue)

    async def publish(self, task_id: int, stage: int, message: str) -> None:
        created = datetime.now(UTC).isoformat()
        with connect() as conn:
            conn.execute(
                "INSERT INTO events(task_id, stage, message, created_at, is_activity) VALUES (?, ?, ?, ?, 1)",
                (task_id, stage, message, created),
            )
        event = json.dumps({"task_id": task_id, "stage": stage, "message": message, "created_at": created})
        for queue in list(self.subscribers[task_id]):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = EventBus()
