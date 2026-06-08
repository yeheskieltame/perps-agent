"""ShardRouter — consistent hashing: deterministic, ~even, minimal remap on resize.
Plus the AppService shard guard (a worker serves/recovers only its own users)."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.app.shard import ShardRouter
from perpsagent.domain.models import GridConfig, Spacing, Venue


def cfg(instance: str) -> GridConfig:
    return GridConfig(instance, Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


def test_deterministic_across_instances():
    a, b = ShardRouter(4), ShardRouter(4)
    for uid in range(200):
        assert a.route(uid) == b.route(uid)        # stable (hashlib, not salted hash)
        assert a.owns(uid, a.route(uid))


def test_distribution_is_roughly_even():
    r = ShardRouter(4)
    counts = {n: 0 for n in r.nodes}
    for uid in range(8000):
        counts[r.route(uid)] += 1
    ideal = 8000 / 4
    for n, c in counts.items():
        assert 0.6 * ideal < c < 1.4 * ideal, (n, c)   # within ±40% of even


def test_resize_remaps_minimally():
    before = ShardRouter(4)
    after = ShardRouter(5)                              # add one shard
    moved = sum(before.route(uid) != after.route(uid) for uid in range(8000))
    # consistent hashing moves ~1/5; modulo would move ~80%. Assert well under that.
    assert 0 < moved < 0.35 * 8000


def test_invalid_shard_count():
    with pytest.raises(ValueError):
        ShardRouter(0)


@pytest.mark.asyncio
async def test_appservice_rejects_user_off_shard():
    r = ShardRouter(["w0", "w1"])
    # find a user each shard owns, so the test is independent of hash specifics
    u_w0 = next(u for u in range(1000) if r.owns(u, "w0"))
    u_w1 = next(u for u in range(1000) if r.owns(u, "w1"))

    svc = AppService(client_factory=lambda uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                     router=r, node="w0")
    await svc.create_grid(u_w0, cfg("a1"))             # owned → ok
    assert [v.instance_id for v in await svc.status(u_w0)] == ["a1"]
    with pytest.raises(PermissionError):
        await svc.create_grid(u_w1, cfg("b1"))         # not this shard's user
    with pytest.raises(PermissionError):
        await svc.balance(u_w1)
    await svc.disconnect(u_w0)


@pytest.mark.asyncio
async def test_recover_only_rebuilds_owned_users(tmp_path):
    from perpsagent.adapters.store.sqlite_store import SqliteStore

    r = ShardRouter(["w0", "w1"])
    u_w0 = next(u for u in range(1000) if r.owns(u, "w0"))
    u_w1 = next(u for u in range(1000) if r.owns(u, "w1"))

    store = SqliteStore(str(tmp_path / "shard.db"))
    factory = lambda uid: FakeExchange({"mid": "100", "tick": "0.1"})  # noqa: E731
    seed = AppService(store=store, client_factory=factory)             # no shard filter: place both
    await seed.create_grid(u_w0, cfg("a1"))
    await seed.create_grid(u_w1, cfg("b1"))
    await seed.disconnect(u_w0)
    await seed.disconnect(u_w1)

    # worker w0 recovers from the SAME store → only its own user's grid
    w0 = AppService(store=SqliteStore(str(tmp_path / "shard.db")), client_factory=factory,
                    router=r, node="w0")
    rebuilt = await w0.recover()
    assert rebuilt == [(u_w0, "a1")]                   # b1 belongs to w1, skipped
    await w0.disconnect(u_w0)
