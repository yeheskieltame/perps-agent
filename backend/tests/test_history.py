"""Closing a grid drops it from the active list and records it to history; a
lingering HALTED grid can be cleared. FakeExchange + SqliteStore (no keys/network)."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.sqlite_store import SqliteStore
from perpsagent.app.service import AppService
from perpsagent.domain.models import GridConfig, Spacing, Venue


def _cfg(iid: str):
    return GridConfig(iid, Venue.FAKE, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                      Decimal("0.001"), Spacing.GEOMETRIC)


def _svc(store):
    return AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "tick": "0.1"}), store=store)


@pytest.mark.asyncio
async def test_stop_declutters_and_records_history(tmp_path):
    store = SqliteStore(str(tmp_path / "h.db"))
    svc = _svc(store)
    iid = await svc.create_grid(7, _cfg("BTCUSDT-0-a"))
    assert len(await svc.status(7)) == 1

    await svc.stop_grid(7, iid)
    assert await svc.status(7) == []                       # gone from the active list
    hist = await svc.history(7)
    assert len(hist) == 1
    assert hist[0]["instance_id"] == iid and hist[0]["market"] == "BTCUSDT"
    await store.close()


@pytest.mark.asyncio
async def test_clear_stopped_removes_lingering_halted(tmp_path):
    store = SqliteStore(str(tmp_path / "h2.db"))
    svc = _svc(store)
    a = await svc.create_grid(7, _cfg("BTCUSDT-0-a"))
    b = await svc.create_grid(7, _cfg("BTCUSDT-0-b"))

    session = await svc._session(7)                        # halt one WITHOUT going through stop_grid
    await session.manager.get(a).stop()
    assert len(await svc.status(7)) == 2                   # still listed (HALTED, lingering)

    assert await svc.clear_stopped(7) == 1
    ids = [s.instance_id for s in await svc.status(7)]
    assert a not in ids and b in ids                       # the running one stays
    await store.close()


@pytest.mark.asyncio
async def test_history_empty_without_store():
    svc = AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "tick": "0.1"}))
    assert await svc.history(7) == []
    assert await svc.clear_stopped(7) == 0                 # no session yet
