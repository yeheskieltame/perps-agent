"""Safety — circuit breaker, kill switch, risk caps. Mitigates volatility,
liquidation, and runaway execution (BGA 'Strategy design & risk management').

The engine checks the breaker on every fill and on each monitor tick. When a cap
is breached the breaker trips; the engine then cancels all orders and flattens
the position (emergency exit). A cap <= 0 disables that check.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class CircuitBreaker:
    """Risk caps that halt a grid before runaway loss.

    max_inventory: max absolute net base position (qty) the grid may hold. Guards
        the exact failure mode where price trends out of the band and every BUY
        fills into a one-sided bag.
    max_drawdown:  max tolerated loss (realized + unrealized), as a POSITIVE
        quote number; trips when pnl < -max_drawdown.
    """

    max_inventory: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    tripped: bool = False
    reason: str = ""

    def check(self, net_inventory: Decimal, pnl: Decimal) -> str | None:
        """Return a trip reason if any cap is breached, else None. Idempotent once
        tripped. `pnl` is realized (+ unrealized when a mark is available)."""
        if self.tripped:
            return self.reason
        if self.max_inventory > 0 and abs(net_inventory) > self.max_inventory:
            return self.trip(f"inventory {abs(net_inventory)} > cap {self.max_inventory}")
        if self.max_drawdown > 0 and pnl < -self.max_drawdown:
            return self.trip(f"drawdown {pnl} < -{self.max_drawdown}")
        return None

    def trip(self, reason: str) -> str:
        self.tripped = True
        self.reason = reason
        return reason

    def reset(self) -> None:
        self.tripped = False
        self.reason = ""
