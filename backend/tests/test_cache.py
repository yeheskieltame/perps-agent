"""InProcessCache (async CachePort) — hit, expiry, disable (deterministic clock)."""
import pytest

from perpsagent.app.cache import InProcessCache


@pytest.mark.asyncio
async def test_hit_then_expire():
    now = [100.0]
    c = InProcessCache(ttl_s=5.0, clock=lambda: now[0])
    assert await c.get("k") is None        # miss
    await c.put("k", {"v": 42})
    assert await c.get("k") == {"v": 42}   # hit
    now[0] += 4.9
    assert await c.get("k") == {"v": 42}   # still fresh
    now[0] += 0.2                          # 5.1s elapsed > 5s ttl
    assert await c.get("k") is None        # expired


@pytest.mark.asyncio
async def test_zero_ttl_disables():
    c = InProcessCache(ttl_s=0.0, clock=lambda: 0.0)
    await c.put("k", 1)
    assert await c.get("k") is None        # ttl<=0 → nothing is cached
