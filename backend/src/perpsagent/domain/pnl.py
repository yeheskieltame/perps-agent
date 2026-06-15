"""Pure performance metrics.

The optimization target is *risk-adjusted*, not raw PnL — Perps Agent rewards
"better systems, not the highest PnL" (docs.perpsagent.xyz). `recall` ranks past
episodes by the same score.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal


def winrate(pnls: Sequence[Decimal]) -> float:
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)


def max_drawdown(equity_curve: Sequence[Decimal]) -> float:
    peak: Decimal | None = None
    mdd = 0.0
    for e in equity_curve:
        peak = e if peak is None else max(peak, e)
        if peak and peak > 0:
            mdd = max(mdd, float((peak - e) / peak))
    return mdd


def risk_adjusted(realized_pnl: Decimal, mdd: float) -> float:
    """PnL-per-drawdown: a simple, explainable risk-adjusted score.

    TODO: upgrade to Sortino over the per-episode return series.
    """
    if mdd <= 0:
        return float(realized_pnl)
    return float(realized_pnl) / mdd
