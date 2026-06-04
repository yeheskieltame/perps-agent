"""SQLite store persistence + engine recovery tests."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.sqlite_store import SqliteStore
from perpsagent.app.engine import GridEngine
from perpsagent.app.manager import GridManager
from perpsagent.domain.models import Fill, GridConfig, Side, Spacing, Venue


def _cfg():
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


def _fills():
    return [
        Fill("i1", "BTCUSDT", Side.BUY, Decimal("98"), Decimal("0.01"), "grid-i1-L4-0", 1, level=4),
        Fill("i1", "BTCUSDT", Side.SELL, Decimal("100"), Decimal("0.01"), "grid-i1-L5-0", 2, level=5),
    ]


@pytest.mark.asyncio
async def test_persist_and_reload_across_connections(tmp_path):
    db = str(tmp_path / "t.db")
    store = SqliteStore(db)
    await store.save_instance(_cfg())
    for f in _fills():
        await store.record_fill(f)

    store2 = SqliteStore(db)  # reopen → durable
    opens = await store2.load_open_instances()
    assert len(opens) == 1 and opens[0].instance_id == "i1" and opens[0].lower == Decimal("90")
    fills = await store2.load_fills("i1")
    assert len(fills) == 2 and fills[0].side is Side.BUY and fills[1].price == Decimal("100")

    await store2.set_state("i1", "CLOSED")
    assert list(await store2.load_open_instances()) == []


@pytest.mark.asyncio
async def test_engine_rehydrate_restores_pnl(tmp_path):
    store = SqliteStore(str(tmp_path / "r.db"))
    await store.save_instance(_cfg())
    for f in _fills():
        await store.record_fill(f)
    eng = GridEngine(FakeExchange({"mid": "100"}), _cfg(), store)
    eng.rehydrate(await store.load_fills("i1"))
    assert eng.realized == Decimal("0.02") and eng.winrate == 1.0 and eng.fill_count == 2


@pytest.mark.asyncio
async def test_manager_recover(tmp_path):
    store = SqliteStore(str(tmp_path / "m.db"))
    await store.save_instance(_cfg())
    for f in _fills():
        await store.record_fill(f)
    engines = await GridManager().recover(store, FakeExchange({"mid": "100"}))
    assert len(engines) == 1 and engines[0].realized == Decimal("0.02")
