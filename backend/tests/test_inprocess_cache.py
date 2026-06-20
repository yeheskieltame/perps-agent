"""InProcessCache is bounded (LRU) so a flood of distinct {market} cache keys can't
grow memory without limit, and TTL still expires entries."""
import pytest

from perpsagent.app.cache import InProcessCache


@pytest.mark.asyncio
async def test_evicts_least_recently_used_over_max():
    c = InProcessCache(ttl_s=1000, max_entries=3)
    for i in range(5):
        await c.put(f"k{i}", i)
    assert await c.get("k0") is None and await c.get("k1") is None  # oldest evicted
    assert [await c.get(k) for k in ("k2", "k3", "k4")] == [2, 3, 4]
    assert len(c._d) == 3  # never grows past the cap


@pytest.mark.asyncio
async def test_get_refreshes_recency():
    c = InProcessCache(ttl_s=1000, max_entries=2)
    await c.put("a", 1)
    await c.put("b", 2)
    assert await c.get("a") == 1   # 'a' becomes most-recently used
    await c.put("c", 3)            # evicts the LRU, which is now 'b'
    assert await c.get("b") is None and await c.get("a") == 1 and await c.get("c") == 3


@pytest.mark.asyncio
async def test_ttl_expiry_still_works():
    t = {"now": 0.0}
    c = InProcessCache(ttl_s=5, clock=lambda: t["now"], max_entries=10)
    await c.put("k", "v")
    t["now"] = 6.0
    assert await c.get("k") is None  # expired past TTL
