"""Sliding-window rate limiter.

In-memory and per-process: correct for a single uvicorn worker. If you run several
workers/instances, swap `InMemoryRateLimiter` for a Redis-backed implementation with
the same `hit()` signature.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request, status

from .config import get_settings


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: float) -> bool:
        """Record a hit; return False if the key is over its limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 100_000:  # crude memory bound
                self._gc(now, window_seconds)
            return True

    def _gc(self, now: float, window: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - window]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = InMemoryRateLimiter()

TOO_MANY = HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                         detail="Too many requests. Please wait and try again.",
                         headers={"Retry-After": "60"})


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def limit_ip(request: Request) -> None:
    """FastAPI dependency: per-IP limit for every auth endpoint."""
    s = get_settings()
    if not limiter.hit(f"ip:{client_ip(request)}", s.rate_limit_ip_per_minute, 60):
        raise TOO_MANY


def limit_account(identifier: str) -> None:
    s = get_settings()
    if not limiter.hit(f"acct:{identifier.lower()}", s.rate_limit_account_per_15min, 15 * 60):
        raise TOO_MANY
