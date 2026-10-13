"""Server-Sent Events broadcaster for live telemetry and experiment updates."""

import asyncio
from typing import Any, Dict, List


class EventBroadcaster:
    """Manages SSE client queues and broadcasts state updates to the UI."""

    def __init__(self) -> None:
        self.subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self.subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    async def broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        payload = {"event": event_type, "data": data}
        async with self._lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Drop stale events for sluggish consumers
                    pass


# Global singleton instance
broadcaster = EventBroadcaster()
