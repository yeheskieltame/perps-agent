"""Pure regime helpers: turn a RegimeFingerprint into a stable, bucketed key the
agent passes to StrategyMemory on-chain (the k-NN key). Coarse bucketing groups
'similar' regimes so recall finds analogous past episodes (docs/CONCEPT.md §3)."""
from __future__ import annotations

import hashlib

from .models import RegimeFingerprint


def bucket(fp: RegimeFingerprint) -> tuple:
    """Coarse, deterministic bucketing of continuous features."""
    return (
        round(fp.realized_vol, 2),
        round(fp.trend_strength, 1),
        round(fp.funding_rate, 4),
        round(fp.range_width, 2),
        round(fp.smart_money_flow, 1),
    )


def regime_key(fp: RegimeFingerprint) -> str:
    """A bytes32-shaped hex key for the bucketed regime. Deterministic so the
    same regime maps to the same on-chain slot. (On-chain we mirror this as a
    keccak of the same tuple; here stdlib sha256 keeps the domain dependency-free.)
    """
    raw = repr(bucket(fp)).encode()
    return "0x" + hashlib.sha256(raw).hexdigest()
