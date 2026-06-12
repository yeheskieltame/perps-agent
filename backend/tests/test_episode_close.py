"""Episode-close correctness (live incidents 2026-06-10/11):
- a graceful stop must flatten leftover inventory (99 MNT survived a close with
  zero TP orders) and book the exit into realized PnL;
- a guard exit's flatten must be counted too (a -0.354 breaker flatten that
  outcome() never saw let an episode attest 'winrate 100%' while losing money);
- the runner must never announce 'attested' when the attest tx failed.
All on FakeExchange / MemoryChain — no keys, no network."""
import asyncio
from decimal import Decimal

import pytest

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.agent.loop import LearningLoop
from perpsagent.app.engine import GridEngine
from perpsagent.app.manager import GridManager
from perpsagent.app.safety import CircuitBreaker
from perpsagent.domain.models import GridConfig, GridState, Spacing, Venue


def cfg(instance="i1", market="BTCUSDT"):
    return GridConfig(
        instance, Venue.FAKE, market, Decimal("90"), Decimal("110"), 11,
        Decimal("0.01"), Spacing.ARITHMETIC,
    )


async def _drain():
    for _ in range(100):
        await asyncio.sleep(0)


# ---- stop() flattens and books the exit ----

@pytest.mark.asyncio
async def test_stop_flattens_leftover_inventory_and_books_it():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    eng.pos_qty = Decimal("0.05")            # long survived the episode
    eng.pos_avg = Decimal("99")
    await eng.stop()
    assert ex.flatten_calls == ["BTCUSDT"]   # venue position actually closed
    assert eng.pos_qty == 0
    assert eng.realized == Decimal("0.05")   # (100 - 99) * 0.05 booked at mid
    assert eng.state is GridState.HALTED


@pytest.mark.asyncio
async def test_stop_when_flat_does_not_touch_the_position():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    await eng.stop()
    assert ex.flatten_calls == []            # nothing to flatten, nothing nuked


# ---- guard exits book the flatten cost ----

@pytest.mark.asyncio
async def test_breaker_flatten_cost_lands_in_realized_pnl():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg(), breaker=CircuitBreaker(max_drawdown=Decimal("0.4")))
    eng._exit_retry_delay = 0
    await eng.start()
    eng.pos_qty = Decimal("0.05")            # long, then price collapses
    eng.pos_avg = Decimal("100")
    await eng._check_guards(Decimal("90"))   # unrealized -0.5 < -0.4 -> trip
    assert eng.exit_kind == "circuit-breaker"
    assert eng.realized == Decimal("-0.5")   # the flatten leg is real PnL now
    assert eng.pos_qty == 0
    out = eng.outcome()
    assert out.realized_pnl == Decimal("-0.5")
    assert eng.winrate < 1.0                 # the losing exit counts as a close


@pytest.mark.asyncio
async def test_failed_flatten_keeps_position_on_the_books():
    class RefusesFlatten(FakeExchange):
        async def flatten(self, market):
            raise RuntimeError("venue down")

    ex = RefusesFlatten({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg(), breaker=CircuitBreaker(max_drawdown=Decimal("0.4")))
    eng._exit_retry_delay = 0
    await eng.start()
    eng.pos_qty = Decimal("0.05")
    eng.pos_avg = Decimal("100")
    await eng._check_guards(Decimal("90"))
    assert eng.pos_qty == Decimal("0.05")    # NOT zeroed: the venue still holds it
    assert eng.realized == 0                 # and no phantom PnL was booked


# ---- truthful attest reporting ----

@pytest.mark.asyncio
async def test_close_and_learn_reports_attest_failure():
    class NonceRace(MemoryChain):
        async def attest(self, instance_id, outcome):
            raise RuntimeError("nonce too low: next nonce 75, tx nonce 74")

    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    loop = LearningLoop(ex, NonceRace(), GridManager())
    iid, _, _ = await loop.plan_and_launch("BTCUSDT")
    await _drain()
    await loop.close_and_learn(iid)
    assert loop.last_attest_ok is False      # runner prints the recovery hint

    ok_loop = LearningLoop(ex, MemoryChain(), GridManager())
    iid2, _, _ = await ok_loop.plan_and_launch("BTCUSDT")
    await _drain()
    await ok_loop.close_and_learn(iid2)
    assert ok_loop.last_attest_ok is True
