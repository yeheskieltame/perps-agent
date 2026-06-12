"""Structure-aware launch (live 2026-06-12, LAB ep 2): the bot used to center
every grid on the launch price and trust the trend read alone — launching
right after a rally leg put 'buy the dip' at the structural top. Now SENSE
reads where price sits in the recent window (range_position), DECIDE vetoes
with-trend entries at range exhaustion, and the grid center is anchored
toward the window midpoint instead of the last tick."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.agent.decide import ContextualPolicy
from perpsagent.agent.sense import classify_regime
from perpsagent.domain.models import RegimeFingerprint


def _regime(trend: float, range_position: float = 0.5) -> RegimeFingerprint:
    return RegimeFingerprint(realized_vol=0.03, trend_strength=trend, funding_rate=0.0,
                             range_width=0.001, volume_z=0.0, range_position=range_position)


# ---- sense: range_position from venue klines ----

@pytest.mark.asyncio
async def test_sense_reads_price_position_in_the_window():
    class TopOfRange(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(100) + Decimal(i) for i in range(60)]   # closed at the high

    class BottomOfRange(FakeExchange):
        async def klines(self, market, interval="1", limit=60):
            return [Decimal(160) - Decimal(i) for i in range(60)]   # closed at the low

    top = await classify_regime(TopOfRange({"mid": "100", "tick": "0.1"}), "X", [])
    assert top.range_position > 0.95
    bottom = await classify_regime(BottomOfRange({"mid": "100", "tick": "0.1"}), "X", [])
    assert bottom.range_position < 0.05
    plain = await classify_regime(FakeExchange({"mid": "100", "tick": "0.1"}), "X", [])
    assert plain.range_position == 0.5          # no klines -> neutral, veto inert


# ---- decide: structure veto on the bias ----

def test_structure_veto_blocks_buying_the_top_and_selling_the_bottom():
    p = ContextualPolicy()
    assert p.bias_for(0.4, range_position=0.9) == 0     # the LAB ep 2 shape
    assert p.bias_for(-0.4, range_position=0.1) == 0    # mirror: selling the lows
    assert p.bias_for(0.4, range_position=0.5) == 1     # mid-range: trend rules
    assert p.bias_for(-0.4, range_position=0.9) == -1   # shorting the TOP is fine


def test_breakout_overrides_the_structure_veto():
    p = ContextualPolicy()
    assert p.bias_for(0.7, range_position=0.95) == 1    # genuine breakout: go
    assert p.bias_for(-0.7, range_position=0.05) == -1


def test_pinned_bias_mode_bypasses_the_veto():
    assert ContextualPolicy(bias_mode="long").bias_for(0.1, range_position=1.0) == 1


# ---- decide: anchored grid center ----

def test_propose_anchors_center_away_from_a_range_extreme():
    p = ContextualPolicy()
    mid = Decimal("100")
    at_top = p.propose("i-1", "X", mid, [], regime=_regime(0.0, range_position=1.0))
    center = (at_top.lower + at_top.upper) / 2
    assert center < mid                          # leaned down toward the range mid
    assert at_top.lower < mid < at_top.upper     # price still inside the band

    at_bottom = p.propose("i-2", "X", mid, [], regime=_regime(0.0, range_position=0.0))
    center = (at_bottom.lower + at_bottom.upper) / 2
    assert center > mid
    assert at_bottom.lower < mid < at_bottom.upper

    mid_range = p.propose("i-3", "X", mid, [], regime=_regime(0.0, range_position=0.5))
    assert (mid_range.lower + mid_range.upper) / 2 == mid


# ---- compat: old persisted regimes (no range_position) still load ----

def test_old_regime_json_defaults_to_neutral_position():
    old = {"realized_vol": 0.1, "trend_strength": 0.2, "funding_rate": 0.0,
           "range_width": 0.001, "volume_z": 0.0, "smart_money_flow": 0.0,
           "social_momentum": 0.0}
    fp = RegimeFingerprint(**old)
    assert fp.range_position == 0.5
