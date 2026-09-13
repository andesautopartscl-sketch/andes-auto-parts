"""Replaceable in-process rate limiter."""
from __future__ import annotations

import time
from collections import deque
from typing import Protocol


class RateLimiter(Protocol):
    def allow(self, key: str) -> bool:
        ...


class InMemoryRateLimiter:
    """Simple sliding-window limiter. Swap later for Redis/shared store."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True


class RateLimitError(Exception):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message)
        self.code = "rate_limited"
        self.message = message
        self.status = 429
