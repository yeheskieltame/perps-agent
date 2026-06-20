"""Cache seam for the alpha API.

`CachePort` is the small async contract the alpha API depends on; `InProcessCache`
is the single-process default (TTL, no infra). A multi-replica deploy swaps in
`RedisCache` (adapters/cache/redis_cache.py) behind the SAME port so the UI fleet
shares one cache instead of stampeding Bybit/Mantle/Surf per replica. Async so the
Redis impl never blocks the event loop. Values must be JSON-serializable (the alpha
API caches response bodies, which are).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Callable, Protocol


class CachePort(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def put(self, key: str, value: Any) -> None: ...


class InProcessCache:
    """Async CachePort, single-process TTL + LRU bound. `ttl_s <= 0` disables caching.

    The cache key is `regime:{market}` / `recall:{market}` where `{market}` comes
    from the request path; an unbounded dict would grow with every distinct (paid)
    market string. `max_entries` caps it — least-recently-used entries are evicted —
    so a flood of distinct markets can't grow memory without bound."""

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic,
                 max_entries: int = 2048) -> None:
        self.ttl = float(ttl_s)
        self._clock = clock
        self._max = max(1, int(max_entries))
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    async def get(self, key: str) -> Any | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        ts, value = entry
        if self.ttl <= 0 or (self._clock() - ts) > self.ttl:
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)  # most-recently used
        return value

    async def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._d[key] = (self._clock(), value)
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)  # evict least-recently used
