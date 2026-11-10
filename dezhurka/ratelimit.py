"""Ограничение частоты запросов по ключу (скользящее окно, в памяти процесса)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.limit = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        q = self._hits[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
