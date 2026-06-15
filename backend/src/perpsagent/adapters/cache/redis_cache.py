"""RedisCache — a CachePort shared across UI replicas (docs.perpsagent.xyz).

Without it, every UI replica keeps its own in-process cache and a request burst
stampedes Bybit/Mantle/Surf N times over. Redis gives one shared, TTL'd cache for
the fleet. Values are JSON (the alpha API caches response bodies); keys are
namespaced. redis is imported lazily (optional `redis` extra), so this module
loads even when the dependency isn't installed.
"""
from __future__ import annotations

import json
from typing import Any


class RedisCache:
    def __init__(self, url: str, ttl_s: float, namespace: str = "perpsagent:cache:") -> None:
        self._url = url
        self.ttl = int(ttl_s)
        self._ns = namespace
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as redis  # lazy: optional dependency

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def get(self, key: str) -> Any | None:
        raw = await (await self._conn()).get(self._ns + key)
        return json.loads(raw) if raw is not None else None

    async def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        await (await self._conn()).set(self._ns + key, json.dumps(value), ex=self.ttl)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
