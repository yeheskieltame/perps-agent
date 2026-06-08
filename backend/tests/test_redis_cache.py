"""RedisCache integration — runs ONLY when REDIS_URL points at a reachable Redis
(otherwise skipped, so the suite stays green without Redis infra).

  docker compose up -d redis
  REDIS_URL=redis://localhost:56379/0 .venv/bin/python -m pytest tests/test_redis_cache.py -q
"""
import os
import uuid

import pytest

_URL = os.environ.get("REDIS_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="set REDIS_URL to run Redis integration tests")


def _ns() -> str:
    return f"perpsagent:test:{uuid.uuid4()}:"  # unique → hermetic, no cross-run leftovers


@pytest.mark.asyncio
async def test_redis_cache_roundtrip_and_ttl():
    from perpsagent.adapters.cache.redis_cache import RedisCache

    cache = RedisCache(_URL, ttl_s=60, namespace=_ns())
    try:
        key = "recall:BTCUSDT"
        assert await cache.get(key) is None            # miss (fresh namespace)
        body = {"market": "BTCUSDT", "episodes": [{"winrate": 0.7}]}
        await cache.put(key, body)
        assert await cache.get(key) == body            # hit (JSON round-trip)
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_redis_cache_zero_ttl_disables():
    from perpsagent.adapters.cache.redis_cache import RedisCache

    cache = RedisCache(_URL, ttl_s=0, namespace=_ns())
    try:
        await cache.put("k", {"v": 1})
        assert await cache.get("k") is None            # ttl<=0 → not stored
    finally:
        await cache.close()
