"""Hexagonal ports — the only contracts the engine and agent depend on.

Adapters implement these Protocols; the domain core and the agent NEVER import a
concrete adapter or any exchange/chain SDK. Add a venue by implementing
`ExchangePort` and registering it in `adapters/exchanges/registry.py`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import AsyncIterator, Protocol, Sequence

from .models import (
    BalanceView,
    EpisodeOutcome,
    Fill,
    GridConfig,
    MarketMeta,
    MemoryQuery,
    MemoryRecord,
    Order,
    Position,
)


class ExchangePort(Protocol):
    """A trading venue (CEX or on-chain DEX). One instance ~ one account/venue."""

    venue: str

    async def market_meta(self, market: str) -> MarketMeta: ...
    async def best_bid_ask(self, market: str) -> tuple[Decimal, Decimal]: ...
    async def set_leverage(self, market: str, leverage: Decimal) -> None:
        """Enforce the user-chosen leverage on the venue before trading. No-op for
        venues without leverage (spot DEX). Must tolerate 'already set'."""
        ...
    async def place_order(self, order: Order) -> Order: ...
    async def cancel_order(self, market: str, order_id: str) -> None: ...
    async def cancel_all(self, market: str) -> None: ...
    async def flatten(self, market: str) -> None:
        """Emergency exit: market-close any open position (reduce-only). Used by
        the circuit breaker and graceful shutdown."""
        ...
    async def open_orders(self, market: str) -> Sequence[Order]: ...
    async def balance(self) -> BalanceView: ...
    async def positions(self) -> Sequence[Position]: ...
    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield fills as they happen (drive grid logic from fills)."""
        ...


class StorePort(Protocol):
    """Local persistence (fills, instances, recovery anchors)."""

    async def record_fill(self, fill: Fill) -> None: ...
    async def load_open_instances(self) -> Sequence[GridConfig]: ...


class ChainPort(Protocol):
    """Mantle on-chain brain: StrategyLedger + StrategyMemory + Vault.

    This is what makes the on-chain layer *strategy*, not just verification:
    `recall` reads verified experience back into the agent's decision, and the
    same records are the policy's training data (docs/CONCEPT.md §3, §4).
    """

    async def commit_strategy(self, instance_id: str, config: GridConfig) -> str:
        """Commit config hash BEFORE trading (pre-commitment). Returns tx hash."""
        ...

    async def attest(self, instance_id: str, outcome: EpisodeOutcome) -> str:
        """Write verified outcome + fills Merkle root. Returns tx hash."""
        ...

    async def write_memory(self, record: MemoryRecord) -> str:
        """Append a regime->params->outcome record to StrategyMemory."""
        ...

    async def recall(self, query: MemoryQuery) -> Sequence[MemoryRecord]:
        """k-NN over verified episodes for the current regime (own + population)."""
        ...


class SignalPort(Protocol):
    """External regime inputs (Elfa real-time awareness, Nansen on-chain intel)."""

    async def snapshot(self, market: str) -> dict: ...
