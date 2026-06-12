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


@dataclass
class ProfitGuard:
    """Locks gains on a favorable move — the symmetric twin of CircuitBreaker
    (which caps loss). Without it the grid rides an unrealized profit and can give
    it all back when price reverses (the 'kok gak TP' problem).

    take_profit: bank when total PnL (realized + unrealized) >= this. 0 disables.
    trail_frac:  once PnL has peaked, bank if it gives back this fraction of the
        peak (e.g. 0.3 = let it run, exit after a 30% pullback from the high). 0
        disables. This is what 'rides up, then locks when it turns'.
    trail_arm:   the peak must exceed this before the trailing stop arms (so noise
        near breakeven doesn't trigger it).
    """

    take_profit: Decimal = Decimal(0)
    trail_frac: Decimal = Decimal(0)
    trail_arm: Decimal = Decimal(0)
    peak: Decimal = Decimal(0)
    armed: bool = False
    tripped: bool = False
    reason: str = ""

    def check(self, pnl: Decimal) -> str | None:
        """Return a bank-now reason if take-profit or trailing-stop fires, else None."""
        if self.tripped:
            return self.reason
        if self.take_profit > 0 and pnl >= self.take_profit:
            return self._trip(f"take-profit: pnl {pnl} >= {self.take_profit}")
        if self.trail_frac > 0:
            if pnl > self.peak:
                self.peak = pnl
            if not self.armed and self.peak > 0 and self.peak >= self.trail_arm:
                self.armed = True
            if self.armed and self.peak > 0 and (self.peak - pnl) >= self.peak * self.trail_frac:
                return self._trip(f"trailing-stop: pnl {pnl} gave back >= {self.trail_frac} of peak {self.peak}")
        return None

    def _trip(self, reason: str) -> str:
        self.tripped = True
        self.reason = reason
        return reason


@dataclass
class AccountGuard:
    """Account-level kill-switch (trader risk framework, 2026-06-12). The
    CircuitBreaker caps ONE episode's loss; wallet equity can still bleed
    across episodes and markets. This guard arms with the wallet equity at
    episode start and trips once live equity has dropped more than `max_drop`
    (quote units) below it — across ALL activity on the account, not just this
    grid. Per-process by design; the cross-day embargo ("no new grids for
    24h") is an operator decision the trip reason points at.

    max_drop <= 0 disables the guard. An unarmed guard (start equity never
    read) never trips — fail-open is correct here because the breaker still
    caps the episode itself."""

    max_drop: Decimal = Decimal(0)
    start_equity: Decimal | None = None
    tripped: bool = False
    reason: str = ""

    def arm(self, equity: Decimal) -> None:
        if self.start_equity is None:
            self.start_equity = equity

    def check(self, equity: Decimal) -> str | None:
        if self.tripped:
            return self.reason
        if self.max_drop <= 0 or self.start_equity is None:
            return None
        drop = self.start_equity - equity
        if drop > self.max_drop:
            self.tripped = True
            self.reason = (f"account equity -{drop} from {self.start_equity} "
                           f"(> cap {self.max_drop}) — stop, review before relaunching")
            return self.reason
        return None
