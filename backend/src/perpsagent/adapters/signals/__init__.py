"""Signal adapters: Elfa (real-time social/news) + Nansen (on-chain smart money).
Both implement SignalPort: `async snapshot(market) -> dict` of normalized signals
(bounded ~(-1, 1)) fused into the RegimeFingerprint by agent/sense.py."""
from __future__ import annotations


def base_symbol(market: str) -> str:
    """'BTCUSDT' -> 'BTC', 'ETH-PERP' -> 'ETH'."""
    s = market.upper().replace("-", "")
    for q in ("USDT", "USDC", "USD", "PERP"):
        if s.endswith(q):
            return s[: -len(q)] or market.upper()
    return s or market.upper()
