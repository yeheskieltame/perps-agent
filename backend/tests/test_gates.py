"""Launch gates (trader risk framework, adopted 2026-06-12): funding gate +
news blackout. Pure functions + LearningLoop integration on fakes."""
from datetime import datetime, timedelta, timezone

import pytest

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.agent.gates import (
    BLACKOUT_AFTER, BLACKOUT_BEFORE, LaunchGated, funding_gate, news_blackout,
    parse_news_events,
)
from perpsagent.agent.loop import LearningLoop
from perpsagent.app.manager import GridManager


# ---- funding gate (pure) ----

def test_funding_gate_passes_benign_rates():
    assert funding_gate(1, 0.0001) == (1, "")     # long pays 0.01%/8h: fine
    assert funding_gate(-1, 0.0001) == (-1, "")   # short EARNS positive funding
    assert funding_gate(0, 0.0005) == (0, "")     # symmetric: no carry side


def test_funding_gate_demotes_paying_bias_to_symmetric():
    bias, note = funding_gate(1, 0.0005)          # long would pay 0.05%/8h
    assert bias == 0 and "demoted" in note
    bias, note = funding_gate(-1, -0.0005)        # short would pay (negative funding)
    assert bias == 0 and "demoted" in note


def test_funding_gate_skips_crowded_extremes():
    with pytest.raises(LaunchGated, match="extreme"):
        funding_gate(1, 0.002)                    # 0.2%/8h: crowded long side
    with pytest.raises(LaunchGated, match="extreme"):
        funding_gate(-1, -0.0015)                 # crowded short side
    with pytest.raises(LaunchGated, match="extreme"):
        funding_gate(0, 0.002)                    # even symmetric: skip the market


# ---- news blackout (pure) ----

def test_news_blackout_window():
    ev = datetime(2026, 6, 12, 12, 30, tzinfo=timezone.utc)
    inside_before = ev - BLACKOUT_BEFORE + timedelta(minutes=1)
    inside_after = ev + BLACKOUT_AFTER - timedelta(minutes=1)
    outside = ev + BLACKOUT_AFTER + timedelta(minutes=5)
    assert news_blackout(inside_before, [ev])
    assert news_blackout(inside_after, [ev])
    assert news_blackout(ev, [ev])
    assert news_blackout(outside, [ev]) is None
    assert news_blackout(ev, []) is None
    assert news_blackout(ev, None) is None


def test_parse_news_events_formats():
    evs = parse_news_events("2026-06-12T12:30:00Z, 2026-06-18T18:00:00+00:00 ,")
    assert len(evs) == 2
    assert all(e.tzinfo is not None for e in evs)
    assert parse_news_events("") == []


# ---- LearningLoop integration ----

class _Sig:
    def __init__(self, **snap):
        self._snap = snap

    async def snapshot(self, market):
        return self._snap


@pytest.mark.asyncio
async def test_launch_gated_on_extreme_funding_before_commit():
    chain = MemoryChain()
    loop = LearningLoop(FakeExchange({"mid": "100", "tick": "0.1"}), chain, GridManager(),
                        signals=[_Sig(trend_strength=0.6, funding_rate=0.002)])
    with pytest.raises(LaunchGated, match="extreme"):
        await loop.plan_and_launch("BTCUSDT")
    assert not chain._commits                # vetoed BEFORE the on-chain commit


@pytest.mark.asyncio
async def test_paying_bias_demoted_and_noted_in_rationale():
    loop = LearningLoop(FakeExchange({"mid": "100", "tick": "0.1"}), MemoryChain(),
                        GridManager(),
                        signals=[_Sig(trend_strength=0.6, funding_rate=0.0005)])
    iid, cfg, _ = await loop.plan_and_launch("BTCUSDT")
    assert cfg.bias == 0                     # trend said long; carry said no
    assert "funding gate" in loop.rationale(iid)


@pytest.mark.asyncio
async def test_harvesting_bias_passes_untouched():
    loop = LearningLoop(FakeExchange({"mid": "100", "tick": "0.1"}), MemoryChain(),
                        GridManager(),
                        signals=[_Sig(trend_strength=-0.6, funding_rate=0.0005)])
    iid, cfg, _ = await loop.plan_and_launch("BTCUSDT")
    assert cfg.bias == -1                    # short EARNS the positive funding


@pytest.mark.asyncio
async def test_news_blackout_gates_launch():
    now_event = datetime.now(timezone.utc)
    loop = LearningLoop(FakeExchange({"mid": "100", "tick": "0.1"}), MemoryChain(),
                        GridManager(), news_events=[now_event])
    with pytest.raises(LaunchGated, match="blackout"):
        await loop.plan_and_launch("BTCUSDT")
