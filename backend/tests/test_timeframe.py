"""User-selectable operating timeframe (operator insight, 2026-06-12): the grid
must sense structure on the bars where its pattern lives — a 1m sensor on a 1h
structure reads micro-noise as regime shifts and flaps the bias. The timeframe
is a USER choice that flows into SENSE (interval + vol normalization) and the
engine's thesis-break window."""
import math
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.agent.loop import LearningLoop
from perpsagent.agent.sense import classify_regime, local_trend_vol, tf_minutes
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import CircuitBreaker
from perpsagent.domain.models import GridConfig, Spacing, Venue


def cfg():
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


def test_tf_minutes_maps_bybit_interval_codes():
    assert tf_minutes("1") == 1.0
    assert tf_minutes("15") == 15.0
    assert tf_minutes("60") == 60.0
    assert tf_minutes("240") == 240.0
    assert tf_minutes("D") == 1440.0


def test_vol_normalizes_to_daily_for_any_bar_size():
    closes = [100.0 * (1 + 0.001 * ((-1) ** i)) for i in range(60)]
    _, vol_1m = local_trend_vol(closes, bar_minutes=1)
    _, vol_1h = local_trend_vol(closes, bar_minutes=60)
    # same per-bar noise on coarser bars = calmer market: daily vol scales 1/sqrt(60)
    assert vol_1h == pytest.approx(vol_1m / math.sqrt(60))


@pytest.mark.asyncio
async def test_sense_requests_the_users_timeframe():
    seen = {}

    class Venue15m(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            seen["interval"], seen["limit"] = interval, limit
            return [Decimal(100) + Decimal(i) for i in range(60)]

    await classify_regime(Venue15m({"mid": "100", "tick": "0.1"}), "X", [], timeframe="15")
    assert seen["interval"] == "15"


@pytest.mark.asyncio
async def test_loop_threads_timeframe_into_sense():
    seen = {}

    class Venue1h(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            seen["interval"] = interval
            return [Decimal(100)] * 60

    from perpsagent.adapters.chain.memory_chain import MemoryChain
    from perpsagent.app.manager import GridManager
    loop = LearningLoop(Venue1h({"mid": "100", "tick": "0.1"}), MemoryChain(),
                        GridManager(), timeframe="60")
    await loop.plan_and_launch("BTCUSDT")
    assert seen["interval"] == "60"


@pytest.mark.asyncio
async def test_thesis_break_reads_the_users_timeframe():
    seen = {}

    class GrindDown(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            seen["interval"] = interval
            return [Decimal(100) - Decimal("0.05") * i for i in range(60)]

    eng = GridEngine(GrindDown({"mid": "100", "tick": "0.1"}), cfg(),
                     breaker=CircuitBreaker(max_inventory=Decimal("0.03")),
                     timeframe="15")
    eng._exit_retry_delay = 0
    await eng.start()
    eng.pos_qty = Decimal("0.03")
    eng.pos_avg = Decimal("100")
    assert await eng._thesis_break() is True
    assert seen["interval"] == "15"
