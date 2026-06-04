"""Safety — circuit breakers, kill switch, risk caps. Mitigates volatility,
liquidation, runaway execution (BGA 'Strategy design & risk management'). TODO.
"""
from __future__ import annotations


class CircuitBreaker:
    def trip(self, reason: str) -> None:
        raise NotImplementedError("TODO: halt instance, cancel_all, alert")
