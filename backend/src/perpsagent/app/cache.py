"""Tiny in-process TTL cache.

The alpha API recomputes the regime (multi-signal fan-out) and recall (on-chain
read) on every paid call; without a cache, a burst of requests stampedes
Bybit/Mantle/Surf. A short TTL keeps answers fresh while collapsing duplicate
work per market. Single-process only (state lives in the worker); a multi-worker
deploy puts this behind Redis — see plan/SCALING.md.
"""
from __future__ import annotations

import time
from typing import Any, Callable


class TTLCache:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = float(ttl_s)
        self._clock = clock
        self._d: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        ts, value = entry
        if self.ttl <= 0 or (self._clock() - ts) > self.ttl:
            self._d.pop(key, None)
            return None
        return value

    def put(self, key: Any, value: Any) -> None:
        if self.ttl > 0:
            self._d[key] = (self._clock(), value)
