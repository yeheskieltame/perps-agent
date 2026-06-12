"""DECIDE — contextual policy: regime + recalled experience -> GridConfig.

Starts as a k-NN-with-priors policy: if the on-chain memory has verified episodes
for this regime, reuse the best one's shape (band width, level count, spacing)
re-centered on the current mid; otherwise fall back to safe defaults. The reward
that ranks recall is RISK-ADJUSTED, never raw PnL (docs/CONCEPT.md §4)."""
from __future__ import annotations

from decimal import Decimal

from ..domain.models import GridConfig, MemoryRecord, RegimeFingerprint, Spacing, Venue

# Trend-mode thresholds with hysteresis: enter at |trend| >= ENTER, leave at
# |trend| < EXIT. The gap stops the grid flapping between shapes on every
# re-center when trend_strength hovers around one number. ENTER sits at 0.3:
# slow grinds read ~0.3-0.6 on the multi-window t-stat (live 2026-06-11 —
# at 0.4 the grid stayed symmetric against a 6% grind until the breaker).
BIAS_ENTER = 0.3
BIAS_EXIT = 0.2


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
        bias_mode: str = "auto",  # auto | long | short | neutral
    ) -> None:
        self.version = version
        self.default_band = default_band
        self.default_levels = default_levels
        self.order_size = order_size
        # explicit user choices (CLI flags) win over recalled experience — deterministic tuning
        self.pin_band = pin_band
        self.pin_levels = pin_levels
        self.pin_order_size = pin_order_size
        self.bias_mode = bias_mode

    def pinned(self) -> list[str]:
        """Names of the params the user pinned (override recall). For the rationale."""
        return [n for n, p in (("band", self.pin_band), ("levels", self.pin_levels),
                               ("size", self.pin_order_size)) if p]

    def bias_for(self, trend_strength: float, prev_bias: int = 0) -> int:
        """Map regime trend to a grid bias (+1 up / -1 down / 0 ranging) with
        hysteresis around `prev_bias`. A pinned bias_mode short-circuits."""
        if self.bias_mode == "long":
            return 1
        if self.bias_mode == "short":
            return -1
        if self.bias_mode == "neutral":
            return 0
        if prev_bias != 0:  # already in trend mode: stay until trend clearly dies/flips
            if trend_strength * prev_bias >= BIAS_EXIT:
                return prev_bias
            return self.bias_for_fresh(trend_strength)
        return self.bias_for_fresh(trend_strength)

    @staticmethod
    def bias_for_fresh(trend_strength: float) -> int:
        if trend_strength >= BIAS_ENTER:
            return 1
        if trend_strength <= -BIAS_ENTER:
            return -1
        return 0

    def propose(
        self,
        instance_id: str,
        market: str,
        mid: Decimal,
        recalled: list[MemoryRecord],
        venue: Venue = Venue.FAKE,
        leverage: Decimal = Decimal(1),
        regime: RegimeFingerprint | None = None,
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
            bias=self.bias_for(regime.trend_strength) if regime is not None else
                 (1 if self.bias_mode == "long" else -1 if self.bias_mode == "short" else 0),
        )

    def update(self, memory: list[MemoryRecord]) -> None:
        """LEARN hook. The policy is recall-driven, so 'learning' = more verified
        records improving future recall. A parametric learner would refit here."""
        return None
