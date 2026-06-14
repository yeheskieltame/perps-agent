"""Venue positions view (side / mark / unrealized PnL) + close. FakeExchange only."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.domain.models import Position


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
