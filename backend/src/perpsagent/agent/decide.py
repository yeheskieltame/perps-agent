"""DECIDE — contextual policy: regime + recalled experience -> GridConfig.

Starts as a k-NN-with-priors policy: if the on-chain memory has verified episodes
for this regime, reuse the best one's shape (band width, level count, spacing)
re-centered on the current mid; otherwise fall back to safe defaults. The reward
that ranks recall is RISK-ADJUSTED, never raw PnL (docs/CONCEPT.md §4)."""
from __future__ import annotations

from decimal import Decimal

from ..domain.models import GridConfig, MemoryRecord, Spacing, Venue


class ContextualPolicy:
    def __init__(
        self,
        version: str = "v0",
        default_band: Decimal = Decimal("0.01"),
        default_levels: int = 10,
        order_size: Decimal = Decimal("0.01"),
    ) -> None:
        self.version = version
        self.default_band = default_band
        self.default_levels = default_levels
        self.order_size = order_size

    def propose(
        self,
        instance_id: str,
        market: str,
        mid: Decimal,
        recalled: list[MemoryRecord],
        venue: Venue = Venue.FAKE,
        leverage: Decimal = Decimal(1),
    ) -> GridConfig:
        if recalled:
            best = recalled[0]  # chain.recall returns sorted by risk_adjusted desc
            span = best.config.upper + best.config.lower
            half_band = (best.config.upper - best.config.lower) / span if span > 0 else self.default_band
            levels = best.config.levels
            spacing = best.config.spacing
            order_size = best.config.order_size
        else:
            half_band = self.default_band
            levels = self.default_levels
            spacing = Spacing.GEOMETRIC
            order_size = self.order_size

        lower = mid * (Decimal(1) - half_band)
        upper = mid * (Decimal(1) + half_band)
        return GridConfig(
            instance_id=instance_id,
            venue=venue,
            market=market,
            lower=lower,
            upper=upper,
            levels=levels,
            order_size=order_size,
            spacing=spacing,
            leverage=leverage,
            policy_version=self.version,
        )

    def update(self, memory: list[MemoryRecord]) -> None:
        """LEARN hook. The policy is recall-driven, so 'learning' = more verified
        records improving future recall. A parametric learner would refit here."""
        return None
