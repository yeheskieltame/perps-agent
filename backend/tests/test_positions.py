"""Venue positions view (side / mark / unrealized PnL) + close. FakeExchange only."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.domain.models import GridConfig, Position, Spacing, Venue


def _cfg(iid: str):
    return GridConfig(iid, Venue.FAKE, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                      Decimal("0.001"), Spacing.GEOMETRIC)


@pytest.mark.asyncio
async def test_positions_enriches_side_mark_and_pnl():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    ex._positions["BTCUSDT"] = Position("BTCUSDT", Decimal("-0.001"), Decimal("110"))  # short @110
    svc = AppService(client_factory=lambda _u: ex)

    rows = await svc.positions(7)
    assert len(rows) == 1
    p = rows[0]
    assert p["side"] == "SHORT" and p["market"] == "BTCUSDT"
    assert Decimal(p["mark"]) == Decimal("100")             # mid of 99.9 / 100.1
    assert Decimal(p["pnl"]) == Decimal("0.01")             # (100-110) × -0.001
    assert Decimal(p["pnl_pct"]) == Decimal("9.09")         # 0.01 / (0.001×110) × 100


@pytest.mark.asyncio
async def test_close_position_flattens_on_the_venue():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    svc = AppService(client_factory=lambda _u: ex)
    await svc._session(7)                                    # build the session
    await svc.close_position(7, "BTCUSDT")
    assert ex.flatten_calls == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_zero_size_positions_are_skipped():
    ex = FakeExchange({"mid": "100"})
    ex._positions["ETHUSDT"] = Position("ETHUSDT", Decimal("0"), Decimal("100"))
    svc = AppService(client_factory=lambda _u: ex)
    assert await svc.positions(7) == []


@pytest.mark.asyncio
async def test_open_orders_lists_resting_grid_orders():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    svc = AppService(client_factory=lambda _u: ex)
    await svc.create_grid(7, _cfg("BTCUSDT-0-a"))
    rows = await svc.open_orders(7)
    assert rows and all(r["market"] == "BTCUSDT" for r in rows)
    assert {r["side"] for r in rows} <= {"buy", "sell"}


@pytest.mark.asyncio
async def test_close_all_positions_flattens_every_market():
    ex = FakeExchange({"mid": "100"})
    ex._positions["BTCUSDT"] = Position("BTCUSDT", Decimal("0.1"), Decimal("100"))
    ex._positions["ETHUSDT"] = Position("ETHUSDT", Decimal("-1"), Decimal("50"))
    svc = AppService(client_factory=lambda _u: ex)
    assert await svc.close_all_positions(7) == 2
    assert set(ex.flatten_calls) == {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_cancel_all_orders_stops_grids_first():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    svc = AppService(client_factory=lambda _u: ex)
    await svc.create_grid(7, _cfg("BTCUSDT-0-a"))
    assert len(await svc.status(7)) == 1
    markets = await svc.cancel_all_orders(7)
    assert await svc.status(7) == []          # grids stopped (won't re-place)
    assert markets >= 1


@pytest.mark.asyncio
async def test_panic_stops_grids_and_closes_positions():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    ex._positions["ETHUSDT"] = Position("ETHUSDT", Decimal("1"), Decimal("50"))
    svc = AppService(client_factory=lambda _u: ex)
    await svc.create_grid(7, _cfg("BTCUSDT-0-a"))
    res = await svc.panic(7)
    assert res["grids_stopped"] == 1
    assert await svc.status(7) == [] and "ETHUSDT" in ex.flatten_calls
