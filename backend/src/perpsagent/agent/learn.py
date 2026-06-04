"""LEARN — turn an attested episode into a memory record. Because the buffer is
public on-chain, the agent gains a population-level prior, not just self-history.
(docs/CONCEPT.md §3 step 6)"""
from __future__ import annotations

from ..domain.models import EpisodeOutcome, GridConfig, MemoryRecord, RegimeFingerprint


def to_record(regime: RegimeFingerprint, config: GridConfig, outcome: EpisodeOutcome) -> MemoryRecord:
    return MemoryRecord(regime=regime, config=config, outcome=outcome)
