"""TTLCache — hit, expiry, and the disable switch (deterministic clock)."""
from perpsagent.app.cache import TTLCache


def test_ttl_cache_hit_then_expire():
    now = [100.0]
    c = TTLCache(ttl_s=5.0, clock=lambda: now[0])
    assert c.get("k") is None        # miss
    c.put("k", 42)
    assert c.get("k") == 42          # hit
    now[0] += 4.9
    assert c.get("k") == 42          # still fresh
    now[0] += 0.2                    # 5.1s elapsed > 5s ttl
    assert c.get("k") is None        # expired


def test_ttl_cache_zero_disables():
    c = TTLCache(ttl_s=0.0, clock=lambda: 0.0)
    c.put("k", 1)
    assert c.get("k") is None        # ttl<=0 → nothing is cached
