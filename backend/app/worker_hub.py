import asyncio
import json
from typing import Any

from fastapi import WebSocket


class WorkerHub:
    def __init__(self) -> None:
        self.worker: WebSocket | None = None
        self.lock = asyncio.Lock()

    @property
    def online(self) -> bool:
        return self.worker is not None

    async def connect(self, websocket: WebSocket) -> None:
        async with self.lock:
            if self.worker:
                await self.worker.close(code=1012, reason="Replaced by a new worker")
            self.worker = websocket

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self.lock:
            if self.worker is websocket:
                self.worker = None

    async def send(self, message: dict[str, Any]) -> bool:
        async with self.lock:
            if not self.worker:
                return False
            try:
                await self.worker.send_text(json.dumps(message, ensure_ascii=False))
                return True
            except RuntimeError:
                self.worker = None
                return False


hub = WorkerHub()
