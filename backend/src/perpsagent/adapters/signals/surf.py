"""Surf.AI — crypto intelligence for agents (market + exchange microstructure).
Implements SignalPort. VERIFIED LIVE against api.asksurf.ai.

Endpoints used (Surf Data API, 1 credit each, parallel):
  GET /gateway/v1/market/price?symbol=BTC                          → price (metric rows)
  GET /gateway/v1/market/price-indicator?indicator=rsi&symbol=BTC  → RSI → trend_strength
  GET /gateway/v1/market/price-indicator?indicator=atr&symbol=BTC  → ATR; ATR/price → realized_vol
  GET /gateway/v1/exchange/funding-history?pair=BTCUSDT&limit=1    → funding_rate

Auth: `Authorization: Bearer <key>`. Response shape (SimpleListResponse):
{"data":[{..., "value": x | "funding_rate": x}], "meta": {...}}. Fails soft —
no key / error / non-200 → {} so the agent never crashes on a signal outage.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import base_symbol

_BASE = "https://api.asksurf.ai/gateway/v1"


def first_value(payload: Any, field: str = "value") -> float | None:
    """Surf SimpleListResponse: {"data":[{...,"<field>": x}]} -> x (tolerant)."""
    if isinstance(payload, dict):
        rows = payload.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            v = rows[0].get(field)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def rsi_to_trend(rsi: float) -> float:
    """RSI 0..100 -> trend_strength -1..1 around the neutral 50."""
    return (rsi - 50.0) / 50.0


class SurfSignals:
    def __init__(self, config: dict[str, Any]) -> None:
        self._key = config.get("api_key", "")
        self._base = config.get("base_url", _BASE)
        self._session = None  # lazy, reused across calls (no per-call TLS handshake)

    async def _sess(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._key}", "accept": "application/json"}
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict) -> Any | None:
        try:
            import aiohttp

            session = await self._sess()
            async with session.get(
                f"{self._base}/{path}", params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return None
                return await r.json()
        except Exception:
            return None

    async def snapshot(self, market: str) -> dict:
        if not self._key:
            return {}
        sym = base_symbol(market)
        try:
            price_p, rsi_p, atr_p, fund_p = await asyncio.gather(
                self._get("market/price", {"symbol": sym}),
                self._get("market/price-indicator", {"indicator": "rsi", "symbol": sym}),
                self._get("market/price-indicator", {"indicator": "atr", "symbol": sym}),
                self._get("exchange/funding-history", {"pair": f"{sym}USDT", "limit": "1"}),
            )
            out: dict[str, float] = {}
            rsi = first_value(rsi_p)
            if rsi is not None:
                out["trend_strength"] = rsi_to_trend(rsi)
            fr = first_value(fund_p, "funding_rate")
            if fr is not None:
                out["funding_rate"] = fr
            atr, px = first_value(atr_p), first_value(price_p)
            if atr is not None and px:
                out["realized_vol"] = atr / px
            return out
        except Exception:
            return {}
