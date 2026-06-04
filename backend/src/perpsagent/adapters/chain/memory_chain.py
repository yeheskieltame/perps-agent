"""In-memory ChainPort for tests, dry-run, and backtests.

Mirrors the on-chain StrategyLedger + StrategyMemory semantics without a node:
commits are write-once, attestations require the committing agent, and `recall`
ranks records by risk-adjusted score (better systems, not raw PnL).
"""
from __future__ import annotations

from typing import Sequence

from ...domain.models import EpisodeOutcome, GridConfig, MemoryQuery, MemoryRecord
from ...domain.regime import regime_key


class MemoryChain:
    """Implements ChainPort. See perpsagent.domain.ports.ChainPort."""

    def __init__(self, agent: str = "0xagent") -> None:
        self.agent = agent
        self._commits: dict[str, str] = {}  # instance_id -> configHash
        self._attestations: dict[str, EpisodeOutcome] = {}
        self._memory: dict[str, list[MemoryRecord]] = {}  # regime_key -> records
        self._tx = 0

    def _txhash(self) -> str:
        self._tx += 1
        return f"0xfaketx{self._tx:064x}"[:66]

    async def commit_strategy(self, instance_id: str, config: GridConfig) -> str:
        if instance_id in self._commits:
            raise ValueError(f"already committed: {instance_id}")
        self._commits[instance_id] = _config_hash(config)
        return self._txhash()

    async def attest(self, instance_id: str, outcome: EpisodeOutcome) -> str:
        if instance_id not in self._commits:
            raise ValueError(f"no commitment: {instance_id}")
        self._attestations[instance_id] = outcome
        return self._txhash()

    async def write_memory(self, record: MemoryRecord) -> str:
        key = regime_key(record.regime)
        self._memory.setdefault(key, []).append(record)
        return self._txhash()

    async def recall(self, query: MemoryQuery) -> Sequence[MemoryRecord]:
        key = regime_key(query.regime)
        records = list(self._memory.get(key, []))
        records.sort(key=lambda r: r.outcome.risk_adjusted, reverse=True)
        return records[: query.k]


def _config_hash(cfg: GridConfig) -> str:
    import hashlib

    raw = f"{cfg.market}|{cfg.lower}|{cfg.upper}|{cfg.levels}|{cfg.spacing.value}|{cfg.leverage}|{cfg.policy_version}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()
