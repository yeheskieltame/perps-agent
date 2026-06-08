"""Nansen — on-chain intelligence (smart-money netflows, incl. Mantle). SignalPort.

Auth: header `apiKey`. Base `https://api.nansen.ai`.
Endpoint: POST /api/v1/smart-money/netflow.
Maps the market's base token aggregated 24h net flow (USD) to a bounded
`smart_money_flow` signal in (-1, 1) via tanh. Positive = smart money accumulating.
Fails soft (returns 0). See docs/CONCEPT.md §3 (SENSE)."""
from __future__ import annotations

import math
from typing import Any

from . import base_symbol

_BASE = "https://api.nansen.ai"
# Smart-money supported chains (per Nansen OpenAPI); 'mantle' included.
_DEFAULT_CHAINS = ["all"]


def parse_netflow(payload: Any, symbol: str) -> dict:
    """Pure: sum net_flow_24h_usd over rows matching `symbol` → bounded signal."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {"smart_money_flow": 0.0}
    sym = symbol.upper()
    total = 0.0
    matched = False
    for row in rows:
        if isinstance(row, dict) and str(row.get("token_symbol", "")).upper() == sym:
            v = row.get("net_flow_24h_usd")
            if isinstance(v, (int, float)):
                total += float(v)
                matched = True
    return {"smart_money_flow": math.tanh(total / 1e7) if matched else 0.0}


class NansenSignals:
    def __init__(self, config: dict[str, Any]) -> None:
        self._key = config.get("api_key", "")
        self._base = config.get("base_url", _BASE)
        self._chains = config.get("chains", _DEFAULT_CHAINS)
        self._session = None  # lazy, reused across calls

    async def _sess(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                headers={"apiKey": self._key, "Content-Type": "application/json", "accept": "application/json"}
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def snapshot(self, market: str) -> dict:
        if not self._key:
            return {"smart_money_flow": 0.0}
        sym = base_symbol(market)
        body = {
            "chains": self._chains,
            "filters": {"token_address": sym, "include_native_tokens": True, "include_stablecoins": False},
            "pagination": {"page": 1, "per_page": 25},
            "order_by": [{"field": "net_flow_24h_usd", "direction": "DESC"}],
        }
        try:
            import aiohttp

            s = await self._sess()
            async with s.post(
                f"{self._base}/api/v1/smart-money/netflow",
                json=body, timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    return {"smart_money_flow": 0.0}
                data = await r.json()
            return parse_netflow(data, sym)
        except Exception:
            return {"smart_money_flow": 0.0}
