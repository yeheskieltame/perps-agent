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
    assert p.bias_for(0.45) == 1                  # enter trend mode at >= 0.4
    assert p.bias_for(0.3) == 0                   # 0.3 from neutral: not enough
    assert p.bias_for(0.3, prev_bias=1) == 1      # in-mode: stays until < 0.25
    assert p.bias_for(0.2, prev_bias=1) == 0      # decayed below exit -> ranging
    assert p.bias_for(-0.5, prev_bias=1) == -1    # hard flip is allowed
    assert p.bias_for(-0.45) == -1
    assert p.bias_for(-0.3, prev_bias=-1) == -1


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
