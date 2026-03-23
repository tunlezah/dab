"""In-memory activity log for surfacing scan events and errors to the frontend."""

import asyncio
import time
from collections import deque


class ActivityLog:
    """Ring-buffer of recent activity log entries, queryable by the frontend."""

    MAX_ENTRIES = 200

    def __init__(self) -> None:
        self._entries: deque[dict] = deque(maxlen=self.MAX_ENTRIES)
        self._lock = asyncio.Lock()
        self._seq = 0

    async def add(self, level: str, message: str) -> None:
        """Append a log entry. level is one of: info, warn, error."""
        async with self._lock:
            self._seq += 1
            self._entries.append({
                "seq": self._seq,
                "ts": time.time(),
                "level": level,
                "message": message,
            })

    async def get_since(self, after_seq: int = 0) -> list[dict]:
        """Return entries with seq > after_seq."""
        async with self._lock:
            return [e for e in self._entries if e["seq"] > after_seq]

    async def get_recent(self, count: int = 50) -> list[dict]:
        """Return the most recent N entries."""
        async with self._lock:
            entries = list(self._entries)
            return entries[-count:]

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
