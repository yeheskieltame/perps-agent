"""Dynamic re-center, leverage enforcement, and the circuit breaker — all on the
in-memory FakeExchange (no keys/network)."""
from decimal import Decimal

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import CircuitBreaker
from perpsagent.domain.models import Fill, GridConfig, GridState, Side, Venue


def _cfg(**kw) -> GridConfig:
    base = dict(
        instance_id="t-1", venue=Venue.FAKE, market="BTCUSDT",
        lower=Decimal("99"), upper=Decimal("101"), levels=10, order_size=Decimal("0.01"),
    )
    base.update(kw)
    return GridConfig(**base)


async def test_set_leverage_enforced_on_start():
    ex = FakeExchange({"mid": "100"})
    eng = GridEngine(ex, _cfg(leverage=Decimal("10")))
    await eng.start()
    assert ex.leverage == Decimal("10")  # user choice pushed to the venue


async def test_recenter_follows_price_out_of_band():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    await eng.start()
    assert eng.lower == Decimal("99") and eng.upper == Decimal("101")

    await ex.move_price("BTCUSDT", Decimal("105"))  # price trends above the band
    recentered = await eng.maybe_recenter()

    assert recentered is True
    assert eng.center == Decimal("105")
    assert eng.lower == Decimal("103.95") and eng.upper == Decimal("106.05")  # band shape preserved
    prices = [o.price for o in await ex.open_orders("BTCUSDT")]
    assert prices, "a fresh grid must be resting after re-center"
    assert any(p < 105 for p in prices) and any(p > 105 for p in prices)  # straddles new mid
    assert all(o.external_id.endswith("-1") for o in await ex.open_orders("BTCUSDT"))  # gen-1 ids


async def test_no_recenter_inside_band():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    await eng.start()
    await ex.move_price("BTCUSDT", Decimal("100.5"))  # still inside [99, 101]
    assert await eng.maybe_recenter() is False
    assert eng.center == Decimal("100")


async def test_circuit_breaker_trips_and_flattens():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.02"))  # cap = 2 lots
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()

    for i in range(3):  # 3 * 0.01 = 0.03 net long > 0.02 cap
        await eng.handle_fill(Fill(
            instance_id="t-1", market="BTCUSDT", side=Side.BUY,
            price=Decimal("99"), qty=Decimal("0.01"),
            external_id=f"grid-t-1-L{i}-0", ts=0, level=i,
        ))

    assert breaker.tripped
    assert eng.state is GridState.HALTED
    assert "BTCUSDT" in ex.flatten_calls           # emergency flatten fired
    assert await ex.open_orders("BTCUSDT") == []    # cancel_all ran


async def test_breaker_drawdown_on_unrealized():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_drawdown=Decimal("5"))
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()
    # build a long bag at 100, then mark drops to 90 -> unrealized = -10 < -5
    eng._inv.append((Decimal("100"), Decimal("1")))
    await ex.move_price("BTCUSDT", Decimal("90"))
    await eng.maybe_recenter()
    assert breaker.tripped and eng.state is GridState.HALTED
