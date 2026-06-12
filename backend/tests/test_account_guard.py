"""Account-level kill-switch (trader risk framework, 2026-06-12): the breaker
caps ONE episode's loss; the AccountGuard watches WALLET equity across all
markets/episodes and halts the grid when the account-wide drop passes its cap."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import AccountGuard
from perpsagent.domain.models import GridConfig, GridState, Spacing, Venue


def cfg():
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


class MutableEquity(FakeExchange):
    """FakeExchange whose wallet equity can be moved mid-test."""

    def set_equity(self, value: str) -> None:
        self._equity = Decimal(value)


# ---- guard (pure) ----

def test_account_guard_trips_only_past_the_cap():
    g = AccountGuard(max_drop=Decimal("2"))
    g.arm(Decimal("100"))
    assert g.check(Decimal("99")) is None        # -1: inside the cap
    assert g.check(Decimal("98")) is None        # -2: AT the cap, not past it
    reason = g.check(Decimal("97.9"))            # -2.1: past
    assert reason and "account equity" in reason
    assert g.check(Decimal("100")) == reason     # idempotent once tripped


def test_account_guard_disabled_or_unarmed_never_trips():
    assert AccountGuard(max_drop=Decimal("0")).check(Decimal("1")) is None
    unarmed = AccountGuard(max_drop=Decimal("2"))     # arm() never called
    assert unarmed.check(Decimal("0")) is None        # fails open by design
    g = AccountGuard(max_drop=Decimal("2"))
    g.arm(Decimal("100"))
    g.arm(Decimal("50"))                              # re-arm ignored
    assert g.start_equity == Decimal("100")


# ---- engine integration ----

@pytest.mark.asyncio
async def test_engine_halts_when_account_equity_breaks():
    ex = MutableEquity({"mid": "100", "tick": "0.1", "equity": "100"})
    eng = GridEngine(ex, cfg(), account_guard=AccountGuard(max_drop=Decimal("2")))
    eng._exit_retry_delay = 0
    await eng.start()                            # arms at 100
    ex.set_equity("97")                          # -3 across the wallet (any market)
    await eng.maybe_recenter()                   # first tick reads equity
    assert eng.state is GridState.HALTED
    assert eng.exit_kind == "account-guard"
    assert ex.flatten_calls == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_engine_keeps_running_inside_the_cap():
    ex = MutableEquity({"mid": "100", "tick": "0.1", "equity": "100"})
    eng = GridEngine(ex, cfg(), account_guard=AccountGuard(max_drop=Decimal("2")))
    await eng.start()
    ex.set_equity("99")                          # -1: fine
    await eng.maybe_recenter()
    assert eng.state is GridState.RUNNING


@pytest.mark.asyncio
async def test_equity_read_is_throttled_to_every_nth_tick():
    reads = []

    class CountingEquity(MutableEquity):
        async def balance(self):
            reads.append(1)
            return await super().balance()

    ex = CountingEquity({"mid": "100", "tick": "0.1", "equity": "100"})
    eng = GridEngine(ex, cfg(), account_guard=AccountGuard(max_drop=Decimal("2")))
    await eng.start()
    start_reads = len(reads)                     # start() arms once
    for _ in range(eng.ACCOUNT_CHECK_EVERY * 2):
        await eng.maybe_recenter()
    assert len(reads) - start_reads == 2         # 8 ticks -> 2 equity reads
