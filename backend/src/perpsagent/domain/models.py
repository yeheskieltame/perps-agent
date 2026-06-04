"""Domain models (pure data). Shared vocabulary across engine, adapters, agent."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class Venue(str, Enum):
    BYBIT = "bybit"
    MANTLE_DEX = "mantle_dex"  # iZiSwap (default) — see docs/CONCEPT.md §5
    FAKE = "fake"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Spacing(str, Enum):
    GEOMETRIC = "geometric"
    ARITHMETIC = "arithmetic"


class GridState(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    REBALANCING = "REBALANCING"
    EXITING = "EXITING"
    HALTED = "HALTED"


@dataclass(frozen=True)
class MarketMeta:
    market: str
    tick_size: Decimal
    step_size: Decimal
    min_order_size: Decimal


@dataclass
class GridConfig:
    instance_id: str
    venue: Venue
    market: str
    lower: Decimal
    upper: Decimal
    levels: int
    order_size: Decimal = Decimal("0.01")  # base qty placed per level
    spacing: Spacing = Spacing.GEOMETRIC
    leverage: Decimal = Decimal(1)
    max_levels: int = 180  # safety cap (mirrors deltaperps)
    policy_version: str = "v0"  # provenance for the on-chain commitment


@dataclass
class Order:
    instance_id: str
    market: str
    side: Side
    price: Decimal
    qty: Decimal
    external_id: str  # grid-{instance}-L{level}-{nonce} — recovery anchor
    post_only: bool = True
    level: int = 0
    order_id: Optional[str] = None


@dataclass
class Fill:
    instance_id: str
    market: str
    side: Side
    price: Decimal
    qty: Decimal
    external_id: str
    ts: int
    level: int = 0


@dataclass
class Position:
    market: str
    size: Decimal
    entry_price: Decimal


@dataclass
class BalanceView:
    equity: Decimal
    available: Decimal
    currency: str = "USDT"


@dataclass(frozen=True)
class RegimeFingerprint:
    """Compact, queryable description of market conditions at decision time.

    Bucketed into a `regimeKey` on-chain so `recall` can k-NN over verified
    episodes (docs/CONCEPT.md §3 step 2).
    """
    realized_vol: float
    trend_strength: float  # signed ADX-like
    funding_rate: float
    range_width: float
    volume_z: float
    smart_money_flow: float = 0.0  # Nansen
    social_momentum: float = 0.0  # Elfa


@dataclass
class EpisodeOutcome:
    instance_id: str
    realized_pnl: Decimal
    winrate: float
    max_adverse_excursion: float
    fill_count: int
    fills_merkle_root: str
    risk_adjusted: float  # Sortino / PnL-per-drawdown — the OPTIMIZATION target
    is_backtest: bool = False


@dataclass
class MemoryRecord:
    regime: RegimeFingerprint
    config: GridConfig
    outcome: EpisodeOutcome


@dataclass(frozen=True)
class MemoryQuery:
    regime: RegimeFingerprint
    k: int = 16
    include_population: bool = True  # learn from ALL public agents, not just self
