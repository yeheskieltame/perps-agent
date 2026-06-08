"""AppService durable multi-tenant recovery — restart rebuilds each user's grids
with realized PnL and ownership intact. Uses SqliteStore (the StorePort surface is
identical to PostgresStore), so the recovery logic is verified with no PG."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.sqlite_store import SqliteStore
from perpsagent.app.service import AppService
from perpsagent.domain.models import Fill, GridConfig, Side, Spacing, Venue


def cfg(instance: str) -> GridConfig:
    return GridConfig(instance, Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


def _roundtrip_fills(instance: str) -> list[Fill]:
    return [
        Fill(instance, "BTCUSDT", Side.BUY, Decimal("98"), Decimal("0.01"), f"grid-{instance}-L4-0", 1, level=4),
        Fill(instance, "BTCUSDT", Side.SELL, Decimal("100"), Decimal("0.01"), f"grid-{instance}-L5-0", 2, level=5),
    ]


@pytest.mark.asyncio
async def test_recover_rebuilds_grids_per_user(tmp_path):
    db = str(tmp_path / "durable.db")
    store = SqliteStore(db)
    factory = lambda uid: FakeExchange({"mid": "100", "tick": "0.1"})  # noqa: E731

    # user 1 and user 2 each launch a grid; record a winning round-trip for user 1
    svc = AppService(store=store, client_factory=factory)
    await svc.create_grid(1, cfg("a1"))
    await svc.create_grid(2, cfg("b1"))
    for f in _roundtrip_fills("a1"):
        await store.record_fill(f)
    await svc.disconnect(1)
    await svc.disconnect(2)

    # simulate a restart: brand-new AppService over the SAME store
    store2 = SqliteStore(db)
    svc2 = AppService(store=store2, client_factory=factory)
    rebuilt = await svc2.recover()

    assert sorted(rebuilt) == [(1, "a1"), (2, "b1")]          # both users, correct ownership
    sa = await svc2.status(1)
    assert [v.instance_id for v in sa] == ["a1"]
    assert sa[0].realized_pnl == "0.02"                        # PnL replayed from persisted fills
    assert [v.instance_id for v in await svc2.status(2)] == ["b1"]

    # ownership still enforced after recovery
    with pytest.raises(PermissionError):
        await svc2.stop_grid(2, "a1")
    await svc2.disconnect(1)
    await svc2.disconnect(2)


@pytest.mark.asyncio
async def test_recover_noop_without_owner_aware_store():
    svc = AppService(FakeExchange({"mid": "100"}))   # no store
    assert await svc.recover() == []
