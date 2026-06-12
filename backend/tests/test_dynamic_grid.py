"""Dynamic grid: regime-biased ladders (trend mode), hysteresis on the mode
switch, and cap-aware re-center thinning (regression for issue #34). All on
FakeExchange — no keys/network."""
from decimal import Decimal

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.serde import cfg_to_json, json_to_cfg
from perpsagent.agent.decide import ContextualPolicy
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import CircuitBreaker
from perpsagent.domain.grid import build_grid_orders, plan_levels
from perpsagent.domain.models import GridConfig, RegimeFingerprint, Side, Venue


def _cfg(**kw) -> GridConfig:
    base = dict(
        instance_id="t-1", venue=Venue.FAKE, market="BTCUSDT",
        lower=Decimal("99"), upper=Decimal("101"), levels=10, order_size=Decimal("0.01"),
    )
    base.update(kw)
    return GridConfig(**base)


def _regime(trend: float) -> RegimeFingerprint:
    return RegimeFingerprint(realized_vol=0.03, trend_strength=trend, funding_rate=0.0,
                             range_width=0.001, volume_z=0.0, smart_money_flow=0.0,
                             social_momentum=0.0)


# ---- domain: biased ladders ----

def test_build_grid_orders_bias_shapes_the_ladder():
    cfg = _cfg()
    levels = plan_levels(cfg)
    mid = Decimal("100")
    both = build_grid_orders(cfg, levels, mid)
    assert {o.side for o in both} == {Side.BUY, Side.SELL}

    long_only = build_grid_orders(cfg, levels, mid, bias=1)
    assert long_only and all(o.side is Side.BUY for o in long_only)
    assert all(o.price < mid for o in long_only)        # buy the dips only

    short_only = build_grid_orders(cfg, levels, mid, bias=-1)
    assert short_only and all(o.side is Side.SELL for o in short_only)
    assert all(o.price > mid for o in short_only)       # sell the rips only


# ---- policy: regime -> bias with hysteresis ----

def test_bias_for_hysteresis():
    p = ContextualPolicy()
    assert p.bias_for(0.35) == 1                  # enter trend mode at >= 0.3
    assert p.bias_for(0.25) == 0                  # 0.25 from neutral: not enough
    assert p.bias_for(0.25, prev_bias=1) == 1     # in-mode: stays until < 0.2
    assert p.bias_for(0.15, prev_bias=1) == 0     # decayed below exit -> ranging
    assert p.bias_for(-0.5, prev_bias=1) == -1    # hard flip is allowed
    assert p.bias_for(-0.35) == -1
    assert p.bias_for(-0.25, prev_bias=-1) == -1


def test_bias_mode_pins_override_regime():
    assert ContextualPolicy(bias_mode="long").bias_for(-0.9) == 1
    assert ContextualPolicy(bias_mode="short").bias_for(0.9) == -1
    assert ContextualPolicy(bias_mode="neutral").bias_for(0.9) == 0


def test_propose_stamps_bias_from_regime():
    p = ContextualPolicy()
    up = p.propose("i-1", "BTCUSDT", Decimal("100"), [], regime=_regime(0.6))
    flat = p.propose("i-2", "BTCUSDT", Decimal("100"), [], regime=_regime(0.1))
    down = p.propose("i-3", "BTCUSDT", Decimal("100"), [], regime=_regime(-0.6))
    assert (up.bias, flat.bias, down.bias) == (1, 0, -1)
    legacy = p.propose("i-4", "BTCUSDT", Decimal("100"), [])   # no regime passed
    assert legacy.bias == 0


# ---- serde: bias survives persistence (and old rows default to 0) ----

def test_serde_roundtrips_bias():
    cfg = _cfg(bias=-1)
    assert json_to_cfg(cfg_to_json(cfg)).bias == -1
    legacy_json = cfg_to_json(_cfg()).replace(', "bias": 0', "")
    assert json_to_cfg(legacy_json).bias == 0


# ---- sense: kline fallback when external signals are blind on a market ----

def test_local_trend_vol_reads_a_rally_and_chop():
    from perpsagent.agent.sense import local_trend_vol

    rally = [100 * (1.002 ** i) for i in range(60)]       # persistent +0.2%/bar
    trend, vol = local_trend_vol(rally)
    assert trend > 0.9 and vol > 0

    chop = [100 + (1 if i % 2 else -1) * 0.05 for i in range(60)]  # pure ping-pong
    trend, _ = local_trend_vol(chop)
    assert abs(trend) < 0.2

    dump = [100 * (0.998 ** i) for i in range(60)]
    assert local_trend_vol(dump)[0] < -0.9
    assert local_trend_vol([100.0] * 60) == (0.0, 0.0)    # flat -> zero, no div/0
    assert local_trend_vol([100.0, 101.0]) == (0.0, 0.0)  # too short -> zero


async def test_classify_regime_falls_back_to_venue_klines():
    from perpsagent.agent.sense import classify_regime

    class KlineVenue(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) * (Decimal("1.002") ** i) for i in range(60)]

    regime = await classify_regime(KlineVenue({"mid": "100", "tick": "0.1"}), "HYPEUSDT", [])
    assert regime.trend_strength > 0.4          # enough to engage trend mode
    assert regime.realized_vol > 0
    assert ContextualPolicy().bias_for(regime.trend_strength) == 1

    # a venue with no klines capability keeps the old behaviour (blind zeros)
    plain = await classify_regime(FakeExchange({"mid": "100", "tick": "0.1"}), "HYPEUSDT", [])
    assert plain.trend_strength == 0.0


def test_local_trend_vol_catches_a_slow_grind():
    """Live 2026-06-11: a staircase grind whose per-bar drift hides inside the 1m
    noise must still read as trend — the 4-bar block-mean resample accumulates
    4 bars of drift per return while averaging the noise down. On this series
    the single-window 1m t-stat reads ~0.13 (under the 0.3 bias threshold)."""
    import random

    from perpsagent.agent.sense import local_trend_vol

    rng = random.Random(11)
    grind = [100.0 * (1.0004 ** i) * (1 + rng.gauss(0, 0.0163)) for i in range(60)]
    trend, vol = local_trend_vol(grind)
    assert trend >= 0.3
    assert vol > 0


async def test_classify_regime_fuses_klines_when_externals_are_deaf_to_trend():
    """Live 2026-06-11: externals reported vol!=0 with trend=0 on three grinding
    markets; the old both-zero gate skipped the kline read and the grid stayed
    symmetric against a 6% grind. Venue klines must fuse in regardless."""
    from perpsagent.agent.sense import classify_regime

    class DeafSignal:                       # sees volatility, blind to direction
        async def snapshot(self, market):
            return {"realized_vol": 0.69, "trend_strength": 0.0}

    class KlineVenue(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) * (Decimal("1.002") ** i) for i in range(60)]

    regime = await classify_regime(KlineVenue({"mid": "100", "tick": "0.1"}),
                                   "HOMEUSDT", [DeafSignal()])
    assert regime.trend_strength > 0.4      # venue candles override the deaf zero
    assert regime.realized_vol >= 0.69      # vol keeps the max of both sources


# ---- engine: trend mode lays a one-sided ladder; pairs still take profit ----

async def test_engine_long_bias_places_buy_ladder_only():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg(bias=1))
    await eng.start()
    orders = await ex.open_orders("BTCUSDT")
    assert orders and all(o.side is Side.BUY for o in orders)
    # a filled buy still pairs a SELL above it — take-profit appears organically
    fills = await ex.move_price("BTCUSDT", Decimal("99.2"))
    paired = [await eng.handle_fill(f) for f in fills]
    assert any(p is not None and p.side is Side.SELL for p in paired)


async def test_bias_fn_morphs_grid_on_recenter():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})

    async def to_long(_current: int) -> int:
        return 1

    eng = GridEngine(ex, _cfg(), bias_fn=to_long)
    await eng.start()
    sides = {o.side for o in await ex.open_orders("BTCUSDT")}
    assert sides == {Side.BUY, Side.SELL}            # launched symmetric (ranging)

    await ex.move_price("BTCUSDT", Decimal("105"))   # trend leaves the band
    assert await eng.maybe_recenter() is True
    assert eng.bias == 1
    after = await ex.open_orders("BTCUSDT")
    assert after and all(o.side is Side.BUY for o in after)   # morphed to trend grid


async def test_trend_mode_recenter_at_cap_rearms_take_profits():
    """A long-bias grid at the inventory cap must NOT re-lay an empty book: the
    buy side is thinned (cap) but the held position's take-profit sells must be
    re-armed — bias blocks opening against the trend, never closing."""
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.03"))
    eng = GridEngine(ex, _cfg(bias=1), breaker=breaker)
    await eng.start()
    eng.pos_qty = Decimal("0.03")           # long at the cap
    eng.pos_avg = Decimal("99")
    await ex.move_price("BTCUSDT", Decimal("95"))    # dip exits the band
    assert await eng.maybe_recenter() is True
    mine = [o for o in await ex.open_orders("BTCUSDT") if o.instance_id == "t-1"]
    sells = [o for o in mine if o.side is Side.SELL]
    assert len(sells) == 3                  # one TP per held lot
    assert all(o.side is Side.SELL for o in mine)    # and no cap-busting buys


# ---- engine: re-center can never re-arm the side that breaches the cap (#34) ----

async def test_recenter_at_cap_does_not_rearm_offending_side():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.03"))
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()
    eng.pos_qty = Decimal("-0.03")          # short exactly at the cap
    eng.pos_avg = Decimal("100")
    await ex.move_price("BTCUSDT", Decimal("105"))
    assert await eng.maybe_recenter() is True
    assert not breaker.tripped               # the cap was respected, not breached
    after = [o for o in await ex.open_orders("BTCUSDT") if o.instance_id == "t-1"]
    assert after and all(o.side is Side.BUY for o in after)   # only reducers re-armed


async def test_paired_rebuy_respects_placement_time_cap():
    """Issue #36: the cap is a placement-time budget. A paired re-buy must be
    skipped when position + the RESTING buy ladder + this order could sweep past
    the cap in one fast move (the exact mainnet incident shape)."""
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.03"))
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()                                   # ~5 buys resting below mid
    eng.pos_qty = Decimal("0.02")
    eng.pos_avg = Decimal("100")
    from perpsagent.domain.models import Fill
    paired = await eng.handle_fill(Fill(
        instance_id="t-1", market="BTCUSDT", side=Side.SELL,
        price=Decimal("100.6"), qty=Decimal("0.01"),
        external_id="grid-t-1-L8-0", ts=0, level=8))
    assert paired is None                               # skipped, not placed
    assert not breaker.tripped


async def test_paired_rebuy_places_when_cap_has_room():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("1"))   # ample room
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()
    from perpsagent.domain.models import Fill
    paired = await eng.handle_fill(Fill(
        instance_id="t-1", market="BTCUSDT", side=Side.SELL,
        price=Decimal("100.6"), qty=Decimal("0.01"),
        external_id="grid-t-1-L8-0", ts=0, level=8))
    assert paired is not None and paired.side is Side.BUY


# ---- engine: thesis-break exits capped inventory facing a grind ----

async def test_thesis_break_exits_capped_inventory_against_a_grind():
    """Live 2026-06-11 (ZEC): long at the cap while price grinds straight down —
    exit on the broken thesis instead of donating the gap to the breaker."""
    from perpsagent.domain.models import GridState

    class GrindDown(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) - Decimal("0.05") * i for i in range(60)]  # ER -1

    ex = GrindDown({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg(), breaker=CircuitBreaker(max_inventory=Decimal("0.03")))
    eng._exit_retry_delay = 0
    await eng.start()
    eng.pos_qty = Decimal("0.03")           # long exactly at the cap
    eng.pos_avg = Decimal("100")
    await eng.maybe_recenter()
    assert eng.state is GridState.HALTED
    assert eng.exit_kind == "thesis-break"


async def test_thesis_break_holds_when_grind_favors_or_inventory_light():
    from perpsagent.domain.models import GridState

    class GrindUp(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) + Decimal("0.05") * i for i in range(60)]   # ER +1

    breaker = CircuitBreaker(max_inventory=Decimal("0.03"))
    eng = GridEngine(GrindUp({"mid": "100", "tick": "0.1"}), _cfg(), breaker=breaker)
    await eng.start()
    eng.pos_qty = Decimal("0.03")           # long at cap, grind UP = thesis intact
    eng.pos_avg = Decimal("100")
    assert await eng._thesis_break() is False
    assert eng.state is GridState.RUNNING

    class GrindDown(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) - Decimal("0.05") * i for i in range(60)]

    light = GridEngine(GrindDown({"mid": "100", "tick": "0.1"}), _cfg(),
                       breaker=CircuitBreaker(max_inventory=Decimal("0.03")))
    await light.start()
    light.pos_qty = Decimal("0.01")         # well under the cap: grid still has room
    light.pos_avg = Decimal("100")
    assert await light._thesis_break() is False
    assert light.state is GridState.RUNNING


async def test_recenter_near_cap_thins_to_remaining_room():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    breaker = CircuitBreaker(max_inventory=Decimal("0.03"))
    eng = GridEngine(ex, _cfg(), breaker=breaker)
    await eng.start()
    eng.pos_qty = Decimal("-0.02")          # room for exactly one more sell
    eng.pos_avg = Decimal("50")             # far below the band — avg filter stays inert
    await ex.move_price("BTCUSDT", Decimal("105"))
    assert await eng.maybe_recenter() is True
    sells = [o for o in await ex.open_orders("BTCUSDT")
             if o.instance_id == "t-1" and o.side is Side.SELL]
    assert len(sells) == 1
    all_mine = [o for o in await ex.open_orders("BTCUSDT") if o.instance_id == "t-1"]
    assert min(s.price for s in sells) == min(o.price for o in all_mine if o.side is Side.SELL)
