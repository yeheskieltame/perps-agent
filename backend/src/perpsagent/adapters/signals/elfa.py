"""Elfa.ai — real-time awareness layer (social / news / mentions). SignalPort.

Auth: header `x-elfa-api-key`. Base `https://api.elfa.ai`.
Endpoint: GET /v2/aggregations/trending-tokens?timeWindow=24h (60 req/min PAYG).
Maps the market's base token mention momentum to a bounded `social_momentum`
signal in (-1, 1). Fails soft (returns 0) so the agent never crashes on a signal
outage. See docs.perpsagent.xyz (SENSE).
"""
from __future__ import annotations

import math
from typing import Any

from . import base_symbol

_BASE = "https://api.elfa.ai"


def _num(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def parse_trending(payload: Any, symbol: str) -> dict:
    """Pure: find `symbol` in a trending-tokens payload → bounded social_momentum.
    Tolerant of field-name variants since this endpoint is experimental."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("tokens") or payload.get("result") or []
        if isinstance(items, dict):
            items = items.get("data") or list(items.values())
    if not isinstance(items, list):
        return {"social_momentum": 0.0}
    sym = symbol.upper()
    for it in items:
        if not isinstance(it, dict):
            continue
        tok = str(it.get("token") or it.get("symbol") or it.get("name") or "").upper()
        if sym and sym in tok:
            change = _num(it, "change_percent", "changePercent", "mentionsChangePercent", "change")
            if change is not None:
                return {"social_momentum": math.tanh(change / 100.0)}
            mentions = _num(it, "mentionCount", "mentions", "count")
            if mentions is not None:
                return {"social_momentum": math.tanh(math.log10(1.0 + mentions) / 3.0)}
            return {"social_momentum": 0.0}
    return {"social_momentum": 0.0}


class ElfaSignals:
    def __init__(self, config: dict[str, Any]) -> None:
        self._key = config.get("api_key", "")
        self._base = config.get("base_url", _BASE)
        self._window = config.get("time_window", "24h")
        self._session = None  # lazy, reused across calls

    async def _sess(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                headers={"x-elfa-api-key": self._key, "accept": "application/json"}
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def snapshot(self, market: str) -> dict:
        if not self._key:
            return {"social_momentum": 0.0}
        try:
            import aiohttp

            s = await self._sess()
            async with s.get(
                f"{self._base}/v2/aggregations/trending-tokens",
                params={"timeWindow": self._window}, timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return {"social_momentum": 0.0}
                data = await r.json()
            return parse_trending(data, base_symbol(market))
        except Exception:
            return {"social_momentum": 0.0}
