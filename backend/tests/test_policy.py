"""ContextualPolicy: recalled experience fills params, but explicit user picks win."""
from decimal import Decimal

from perpsagent.agent.decide import ContextualPolicy
from perpsagent.domain.models import (
    EpisodeOutcome, GridConfig, MemoryRecord, RegimeFingerprint, Spacing, Venue,
)


def _recalled():
    cfg = GridConfig(instance_id="prev", venue=Venue.BYBIT, market="BTCUSDT",
                     lower=Decimal("99"), upper=Decimal("101"),  # half-band 0.01
                     levels=20, order_size=Decimal("0.02"), spacing=Spacing.GEOMETRIC)
    regime = RegimeFingerprint(realized_vol=0.0, trend_strength=0.0, funding_rate=0.0,
                               range_width=0.0, volume_z=0.0)
    outcome = EpisodeOutcome(instance_id="prev", realized_pnl=Decimal("1"), winrate=1.0,
                             max_adverse_excursion=0.0, fill_count=1, fills_merkle_root="0x",
                             risk_adjusted=20.0)
    return [MemoryRecord(regime=regime, config=cfg, outcome=outcome)]


def test_recall_fills_params_when_not_pinned():
    pol = ContextualPolicy(default_band=Decimal("0.012"), default_levels=10, order_size=Decimal("0.005"))
    cfg = pol.propose("i", "BTCUSDT", Decimal("100"), _recalled())
    assert cfg.levels == 20 and cfg.order_size == Decimal("0.02")        # recalled shape used
    assert cfg.lower == Decimal("99") and cfg.upper == Decimal("101")    # recalled band (0.01)


def test_pinned_params_override_recall():
    pol = ContextualPolicy(default_band=Decimal("0.012"), default_levels=10, order_size=Decimal("0.005"),
                           pin_band=True, pin_levels=True, pin_order_size=True)
    cfg = pol.propose("i", "BTCUSDT", Decimal("100"), _recalled())
    assert cfg.levels == 10 and cfg.order_size == Decimal("0.005")       # user wins
    assert cfg.lower == Decimal("98.8") and cfg.upper == Decimal("101.2")  # band 0.012, not recalled 0.01
    assert pol.pinned() == ["band", "levels", "size"]


def test_partial_pin_mixes_user_and_recall():
    pol = ContextualPolicy(default_band=Decimal("0.012"), default_levels=10, order_size=Decimal("0.005"),
                           pin_band=True)  # only band pinned
    cfg = pol.propose("i", "BTCUSDT", Decimal("100"), _recalled())
    assert cfg.lower == Decimal("98.8")          # band from user
    assert cfg.levels == 20                       # levels still from recall
    assert cfg.order_size == Decimal("0.02")      # size still from recall
