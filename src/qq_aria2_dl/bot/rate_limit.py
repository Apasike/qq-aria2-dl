from __future__ import annotations

import asyncio
import collections
import time


class SlidingWindowRateLimiter:
    """In-process sliding window limiter; intentionally resets on restart."""

    def __init__(self) -> None:
        self._buckets: dict[str, collections.deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_sec: float) -> bool:
        if limit <= 0:
            return False
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.setdefault(key, collections.deque())
            while bucket and now - bucket[0] > window_sec:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)

            # prune stale keys so the dict cannot grow forever
            if len(self._buckets) > 5000:
                for k in list(self._buckets):
                    b = self._buckets[k]
                    while b and now - b[0] > window_sec:
                        b.popleft()
                    if not b:
                        del self._buckets[k]
            return True