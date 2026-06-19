"""Pure performance metrics.

The optimization target is *risk-adjusted*, not raw PnL — Perps Agent rewards
"better systems, not the highest PnL" (docs.perpsagent.xyz). `recall` ranks past
episodes by the same score.
"""
from __future__ import annotations

from decimal import Decimal


def risk_adjusted(realized_pnl: Decimal, mdd: float) -> float:
    """PnL-per-drawdown: a simple, explainable risk-adjusted score.

    TODO: upgrade to Sortino over the per-episode return series.
    """
    if mdd <= 0:
        return float(realized_pnl)
    return float(realized_pnl) / mdd
