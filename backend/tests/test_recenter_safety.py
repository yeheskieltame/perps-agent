"""Dynamic re-center, leverage enforcement, and the circuit breaker — all on the
in-memory FakeExchange (no keys/network)."""
from decimal import Decimal

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import CircuitBreaker, ProfitGuard
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
    ids = [o.external_id for o in await ex.open_orders("BTCUSDT")]
    assert len(ids) == len(set(ids))  # every re-placed order has a unique id (no reuse)


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
    eng.pos_qty = Decimal("1")
    eng.pos_avg = Decimal("100")
    await ex.move_price("BTCUSDT", Decimal("90"))
    await eng.maybe_recenter()
    assert breaker.tripped and eng.state is GridState.HALTED


def test_profit_guard_take_profit():
    g = ProfitGuard(take_profit=Decimal("100"))
    assert g.check(Decimal("99")) is None
    assert g.check(Decimal("100")) is not None and g.tripped


def test_profit_guard_trailing_locks_after_pullback():
    g = ProfitGuard(trail_frac=Decimal("0.3"), trail_arm=Decimal("50"))
    assert g.check(Decimal("40")) is None      # below arm
    assert g.check(Decimal("100")) is None     # peak 100, armed
    assert g.check(Decimal("75")) is None      # gave back 25 (<30) — keep riding
    assert g.check(Decimal("70")) is not None  # gave back 30 (>=30) — bank
    assert g.tripped


async def test_engine_banks_profit_on_trailing_stop():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    pg = ProfitGuard(trail_frac=Decimal("0.3"), trail_arm=Decimal("1"))
    eng = GridEngine(ex, _cfg(), profit_guard=pg)
    await eng.start()
    eng.pos_qty = Decimal("1")
    eng.pos_avg = Decimal("100")  # long 1 @ 100
    await eng._check_guards(Decimal("110"))   # +10 unrealized -> peak 10, armed
    assert eng.state is GridState.RUNNING
    await eng._check_guards(Decimal("108"))   # gave back 2 (<3) -> hold
    assert eng.state is GridState.RUNNING
    await eng._check_guards(Decimal("106"))   # gave back 4 (>=3) -> bank
    assert pg.tripped and eng.state is GridState.HALTED
    assert "BTCUSDT" in ex.flatten_calls


async def test_recenter_does_not_average_up_into_a_pump():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    await eng.start()
    eng.pos_qty = Decimal("0.05")
    eng.pos_avg = Decimal("100")  # long, avg 100
    await ex.move_price("BTCUSDT", Decimal("110"))   # pump far above avg entry
    await eng.maybe_recenter()
    buys = [o for o in await ex.open_orders("BTCUSDT") if o.side is Side.BUY]
    assert buys == []  # never buy above the average long entry


class _StrictFake(FakeExchange):
    """Like Bybit: rejects a reused orderLinkId forever (err 110072), even after cancel."""
    def __init__(self, cfg=None):
        super().__init__(cfg)
        self._seen_ids: set[str] = set()
        self.rejections = 0

    async def place_order(self, order):
        if order.external_id in self._seen_ids:
            self.rejections += 1
            raise RuntimeError("OrderLinkedID is duplicate")
        self._seen_ids.add(order.external_id)
        return await super().place_order(order)


class _FailOnceFake(FakeExchange):
    """Raises on one chosen placement to simulate a mid-re-center venue error."""
    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.calls = 0
        self.fail_at = None

    async def place_order(self, order):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("venue boom")
        return await super().place_order(order)


async def test_recenter_never_reuses_order_ids():
    ex = _StrictFake({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg(), monitor_interval=1.0)
    await eng.start()
    for f in await ex.move_price("BTCUSDT", Decimal("99.2")):  # buys fill -> paired sells (advance ids)
        await eng.handle_fill(f)
    for px in ["103", "106", "97", "104"]:                     # re-center repeatedly
        await ex.move_price("BTCUSDT", Decimal(px))
        await eng.maybe_recenter()
    assert ex.rejections == 0              # globally-unique ids: never a duplicate
    assert eng.state is GridState.RUNNING


async def test_recenter_survives_place_failure_without_orphaning():
    ex = _FailOnceFake({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg(), monitor_interval=1.0)
    await eng.start()
    ex.fail_at = ex.calls + 2                 # blow up on the 2nd order of the re-center
    await ex.move_price("BTCUSDT", Decimal("105"))
    await eng.maybe_recenter()
    assert eng.state is GridState.RUNNING     # restored, NOT stuck in REBALANCING
    assert await ex.open_orders("BTCUSDT")    # the rest of the grid still got placed


async def test_recenter_cancels_only_own_orders_on_shared_market():
    """Two grids share one market+account: re-centering A must not wipe B."""
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    a = GridEngine(ex, _cfg(instance_id="a"))
    b = GridEngine(ex, _cfg(instance_id="b"))
    await a.start()
    await b.start()
    b_before = {o.external_id for o in await ex.open_orders("BTCUSDT") if o.instance_id == "b"}
    assert b_before
    await a._recenter(Decimal("100.5"))
    after = await ex.open_orders("BTCUSDT")
    assert {o.external_id for o in after if o.instance_id == "b"} == b_before  # B untouched
    assert [o for o in after if o.instance_id == "a"]                          # A re-laid


async def test_stop_cancels_only_own_orders_on_shared_market():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    a = GridEngine(ex, _cfg(instance_id="a"))
    b = GridEngine(ex, _cfg(instance_id="b"))
    await a.start()
    await b.start()
    await a.stop()
    left = await ex.open_orders("BTCUSDT")
    assert [o for o in left if o.instance_id == "a"] == []  # A fully cancelled
    assert [o for o in left if o.instance_id == "b"]        # B keeps trading


class _FlakyExitFake(FakeExchange):
    """flatten fails twice then succeeds — the venue hiccuping mid-emergency."""

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.flatten_attempts = 0

    async def flatten(self, market):
        self.flatten_attempts += 1
        if self.flatten_attempts < 3:
            raise RuntimeError("bybit error 10006: too many visits")
        await super().flatten(market)


async def test_exit_retries_flatten_until_it_lands():
    """A tripped breaker with a live position is unsupervised risk — the exit
    steps must retry through transient venue errors, not log-and-give-up."""
    ex = _FlakyExitFake({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg(), breaker=CircuitBreaker(max_inventory=Decimal("0.01")))
    eng._exit_retry_delay = 0  # no real backoff sleeps in tests
    await eng.start()
    await eng.handle_fill(Fill(instance_id="t-1", market="BTCUSDT", side=Side.BUY,
                               price=Decimal("99"), qty=Decimal("0.02"),
                               external_id="grid-t-1-L0-0", ts=0, level=0))
    assert eng.state is GridState.HALTED
    assert ex.flatten_attempts == 3           # retried through the hiccup
    assert "BTCUSDT" in ex.flatten_calls      # and the position actually closed


async def test_signed_position_tracks_short_from_flat():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    await eng.start()
    # a SELL from flat opens a SHORT — the old long-only model ignored this
    await eng.handle_fill(Fill(instance_id="t-1", market="BTCUSDT", side=Side.SELL,
                               price=Decimal("100"), qty=Decimal("0.03"),
                               external_id="grid-t-1-L7-0", ts=0, level=7))
    assert eng.net_inventory() == Decimal("-0.03")          # signed short
    assert eng.unrealized(Decimal("90")) == Decimal("0.30")  # short gains as price falls 100->90


def test_signed_position_flip_long_to_short_realizes_pnl():
    ex = FakeExchange({"mid": "100"})
    eng = GridEngine(ex, _cfg())
    eng.pos_qty = Decimal("1")
    eng.pos_avg = Decimal("100")  # long 1 @ 100
    eng._apply_fill(Side.SELL, Decimal("110"), Decimal("3"))  # close 1 (+10), flip to short 2 @ 110
    assert eng.realized == Decimal("10")
    assert eng.net_inventory() == Decimal("-2")
    assert eng.pos_avg == Decimal("110")


async def test_breaker_now_catches_a_short_bag():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.02"))
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()
    for i in range(3):  # 3 sells from flat -> short 0.03, abs > 0.02 cap
        await eng.handle_fill(Fill(instance_id="t-1", market="BTCUSDT", side=Side.SELL,
                                   price=Decimal("100"), qty=Decimal("0.01"),
                                   external_id=f"grid-t-1-L{i}-0", ts=0, level=i))
    assert breaker.tripped and eng.state is GridState.HALTED   # short bag is now caught
    assert "BTCUSDT" in ex.flatten_calls
