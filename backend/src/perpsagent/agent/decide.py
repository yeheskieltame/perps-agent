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
        pin_band: bool = False,
        pin_levels: bool = False,
        pin_order_size: bool = False,
    ) -> None:
        self.version = version
        self.default_band = default_band
        self.default_levels = default_levels
        self.order_size = order_size
        # explicit user choices (CLI flags) win over recalled experience — deterministic tuning
        self.pin_band = pin_band
        self.pin_levels = pin_levels
        self.pin_order_size = pin_order_size

    def pinned(self) -> list[str]:
        """Names of the params the user pinned (override recall). For the rationale."""
        return [n for n, p in (("band", self.pin_band), ("levels", self.pin_levels),
                               ("size", self.pin_order_size)) if p]

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
            recalled_band = (best.config.upper - best.config.lower) / span if span > 0 else self.default_band
            half_band = self.default_band if self.pin_band else recalled_band
            levels = self.default_levels if self.pin_levels else best.config.levels
            order_size = self.order_size if self.pin_order_size else best.config.order_size
            spacing = best.config.spacing
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
