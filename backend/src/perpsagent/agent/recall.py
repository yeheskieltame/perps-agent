"""RECALL — query StrategyMemory for the best verified episodes in this regime.
The chain ranks by RISK-ADJUSTED score (better systems, not max PnL). This is the
on-chain layer acting as analysis + strategy. (docs/CONCEPT.md §3 step 2)"""
from __future__ import annotations

from ..domain.models import MemoryQuery, MemoryRecord, RegimeFingerprint


async def recall_best(chain, regime: RegimeFingerprint, k: int = 16) -> list[MemoryRecord]:
    return list(await chain.recall(MemoryQuery(regime=regime, k=k)))
