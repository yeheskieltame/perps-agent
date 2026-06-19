"""Engine-safety guards from the audit:
- a re-center must NOT re-lay a fresh grid when the cancel of the old one failed
  (else the account double-stacks orders + the inventory cap mis-counts);
- a paired take-profit rejection is retried once (a missing exit strands inventory);
- Bybit flatten passes positionIdx (hedge mode) and verifies the position is gone
  before reporting success (a silent reduce-only no-op must not look like a clean
  exit, or the engine books realized PnL that never happened).
All on FakeExchange / monkeypatched adapter — no keys, no network."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.bybit.adapter import BybitExchange
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.domain.models import Fill, GridConfig, GridState, Side, Venue


def _cfg(**kw) -> GridConfig:
    base = dict(instance_id="t-1", venue=Venue.FAKE, market="BTCUSDT",
                lower=Decimal("99"), upper=Decimal("101"), levels=10,
                order_size=Decimal("0.01"))
    base.update(kw)
    return GridConfig(**base)


class _BatchCancelFails(FakeExchange):
    """Venue with batch endpoints whose cancel-batch always fails (e.g. rate-limited)."""

    async def place_orders(self, orders):
        return [await self.place_order(o) for o in orders]

    async def cancel_orders(self, market, orders):
        raise RuntimeError("bybit error 10016: cancel-batch failed")


async def test_recenter_aborts_when_cancel_fails_no_double_stack():
    ex = _BatchCancelFails({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    await eng.start()

    await ex.move_price("BTCUSDT", Decimal("105"))   # leaves band (also fills crossed sells)
    n_resting = len(await ex.open_orders("BTCUSDT"))
    await eng.maybe_recenter()

    # Cancel failed → the OLD grid is left resting and NO new generation is laid on
    # top: generation counter unchanged, order count did not grow, state restored.
    assert eng._gen == 0                                          # no new generation laid
    assert len(await ex.open_orders("BTCUSDT")) == n_resting      # nothing stacked on top
    assert eng.state is GridState.RUNNING


class _FailFirstPlace(FakeExchange):
    """Fails the next `fail_until` single placements, then succeeds — to exercise
    the paired-order retry."""

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.place_calls = 0
        self.fail_until = 0

    async def place_order(self, order):
        self.place_calls += 1
        if self.place_calls <= self.fail_until:
            raise RuntimeError("transient 10016")
        return await super().place_order(order)


async def test_paired_order_retried_once_then_rests():
    ex = _FailFirstPlace({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, _cfg())
    eng._exit_retry_delay = 0  # no real sleep in tests
    await eng.start()

    before = ex.place_calls
    ex.fail_until = ex.place_calls + 1   # the paired order's FIRST attempt fails once
    pair = await eng.handle_fill(
        Fill("t-1", "BTCUSDT", Side.BUY, Decimal("99"), Decimal("0.01"), "grid-t-1-L4-0", 1, level=4)
    )
    assert pair is not None
    assert pair.external_id in eng._resting     # placed on the retry
    assert ex.place_calls == before + 2         # one failed attempt + one success


def _bybit():
    return BybitExchange({"api_key": "k", "api_secret": "s", "testnet": True})


async def test_flatten_raises_when_position_not_closed(monkeypatch):
    ex = _bybit()
    posts = {"n": 0}

    async def fake_get(path, params):  # position stays open on the re-check
        return {"list": [{"symbol": "BTCUSDT", "size": "0.5", "side": "Buy", "positionIdx": 0}]}

    async def fake_post(path, body):
        posts["n"] += 1
        return {}

    monkeypatch.setattr(ex, "_get", fake_get)
    monkeypatch.setattr(ex, "_post", fake_post)
    with pytest.raises(RuntimeError, match="flatten incomplete"):
        await ex.flatten("BTCUSDT")
    assert posts["n"] == 1   # it did attempt the reduce-only close


async def test_flatten_passes_position_idx_and_verifies_closed(monkeypatch):
    ex = _bybit()
    seen = {}
    state = {"n": 0}

    async def fake_get(path, params):
        state["n"] += 1
        if state["n"] == 1:
            return {"list": [{"symbol": "BTCUSDT", "size": "0.5", "side": "Buy", "positionIdx": 0}]}
        return {"list": [{"symbol": "BTCUSDT", "size": "0", "side": "Buy"}]}  # closed on re-check

    async def fake_post(path, body):
        seen.update(body)
        return {}

    monkeypatch.setattr(ex, "_get", fake_get)
    monkeypatch.setattr(ex, "_post", fake_post)
    await ex.flatten("BTCUSDT")  # no raise: position confirmed flat
    assert seen.get("reduceOnly") is True and seen.get("positionIdx") == 0
    assert seen.get("side") == "Sell"  # closing a long
