"""Explainable decision rationale — the auditable 'why' behind each grid episode.

Deterministic by default (no general-LLM dependency): a concise, human-readable
summary derived from the fused regime (Surf microstructure + Elfa social + Nansen
smart-money) + recalled on-chain evidence + chosen params. This keeps the agent
fully explainable on the confirmed credit stack and is what an on-chain decision
log references (docs/CONCEPT.md §4). An optional LLM (e.g. Surf's NL chat) can
enrich this string, but is not required."""
from __future__ import annotations

from ..domain.models import GridConfig, MemoryRecord, RegimeFingerprint


def explain_decision(regime: RegimeFingerprint, recalled: list[MemoryRecord], cfg: GridConfig,
                     overrides: str = "") -> str:
    trend = "trending" if abs(regime.trend_strength) > 0.5 else "ranging"
    bias = "up" if regime.trend_strength > 0.05 else "down" if regime.trend_strength < -0.05 else "flat"
    smart = (
        "accumulating" if regime.smart_money_flow > 0.1
        else "distributing" if regime.smart_money_flow < -0.1 else "neutral"
    )
    social = "elevated" if regime.social_momentum > 0.3 else "calm"
    if recalled:
        b = recalled[0].outcome
        evidence = f"recalled {len(recalled)} verified episode(s); best risk-adj {b.risk_adjusted:.2f}, winrate {b.winrate:.0%}"
        if overrides:
            evidence += f"; user-pinned {overrides} override recall"
    else:
        evidence = "no prior verified episodes in this regime — using safe defaults"
    return (
        f"Regime: {trend} ({bias} bias) | vol={regime.realized_vol:.2f} "
        f"funding={regime.funding_rate:.4f} smart-money={smart} social={social}. "
        f"{evidence}. Plan: grid [{cfg.lower:.4f}, {cfg.upper:.4f}] x{cfg.levels} "
        f"({cfg.spacing.value}), leverage {cfg.leverage}, maker-only, risk-adjusted objective."
    )
