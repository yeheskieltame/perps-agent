"""Domain models (pure data). Shared vocabulary across engine, adapters, agent."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class OrderPlacementError(RuntimeError):
    """A grid launch placed ZERO orders on the venue — every order was rejected
    (typically insufficient margin or below the venue's minimum order value).
    Raised so the launch path surfaces WHY to the user instead of reporting a
    RUNNING grid with no resting orders."""


class Venue(str, Enum):
    BYBIT = "bybit"
    MANTLE_DEX = "mantle_dex"  # iZiSwap (default) — see docs.perpsagent.xyz
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
    # Grid mode for the regime: 0 = symmetric (ranging), +1 = long-bias (uptrend:
    # buy-ladder only, sells appear as paired take-profits), -1 = short-bias.
    bias: int = 0


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
    episodes (docs.perpsagent.xyz).
    """
    realized_vol: float
    trend_strength: float  # signed ADX-like
    funding_rate: float
    range_width: float
    volume_z: float
    smart_money_flow: float = 0.0  # Nansen
    social_momentum: float = 0.0  # Elfa
    # Where the current price sits inside the recent kline window's high-low band
    # (0 = at the lows, 1 = at the highs, 0.5 = mid / unknown). Launching a grid
    # without this is how a "buy the dip" ladder ends up buying the structural
    # top (live 2026-06-12, LABUSDT ep 2).
    range_position: float = 0.5


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
