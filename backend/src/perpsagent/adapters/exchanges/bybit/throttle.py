"""Client-side throttling for the Bybit adapter: an async token-bucket rate
limiter + the retry classification for transient Bybit/HTTP errors.

Bybit v5 enforces per-UID and per-IP request caps (order-create ~10/s/UID). A
single account shared by many grids trips these without client-side pacing, and a
tripped request used to silently drop an order -> a hole in the grid. The limiter
paces requests; retryable failures back off and re-send instead of dropping.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

# Bybit retCodes worth a retry (transient — pace + re-send, never drop):
#   10002 request timestamp expired (clock skew / recvWindow) — re-sign + retry
#   10006 too many visits (per-UID rate limit)
#   10016 internal/server error
#   10018 exceeded IP rate limit
_RETRYABLE_CODES = frozenset({10002, 10006, 10016, 10018})


def is_retryable(http_status: int, ret_code: int) -> bool:
    """Worth retrying: HTTP 429/5xx, or a transient Bybit retCode."""
    if http_status == 429 or http_status >= 500:
        return True
    return ret_code in _RETRYABLE_CODES


class RateLimiter:
    """Async token bucket: `rate` tokens/sec, up to `burst` accumulated. `clock`
    and `sleep` are injectable so the pacing is testable without wall-clock waits.
    `rate <= 0` disables limiting (acquire is a no-op)."""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:  # limiting disabled
            return
        async with self._lock:  # serialize waiters so pacing is exact
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)
