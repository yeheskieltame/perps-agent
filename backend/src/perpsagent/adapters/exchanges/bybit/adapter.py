"""Bybit v5 USDT-perp adapter — the primary execution venue (docs/CONCEPT.md §5).

Non-custodial: the user supplies their own Bybit API keys; capital never leaves
Bybit. Maker-only grids (timeInForce=PostOnly); fills arrive over the private
WebSocket `execution` stream. Testnet by default.

REST signing (v5): X-BAPI-SIGN = HMAC_SHA256(secret, timestamp + apiKey +
recvWindow + (queryString | jsonBody)). WS auth: HMAC_SHA256(secret, "GET/realtime"+expires).

Implemented with aiohttp (already a dependency) so REST + WS share one async stack.
Requires API keys + network to run; the engine is identical to the FakeExchange path.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, AsyncIterator, Sequence

from ....domain.grid import parse_external_id
from ....domain.models import BalanceView, Fill, MarketMeta, Order, Position, Side

_REST = {True: "https://api-testnet.bybit.com", False: "https://api.bybit.com"}
_WS_PRIVATE = {
    True: "wss://stream-testnet.bybit.com/v5/private",
    False: "wss://stream.bybit.com/v5/private",
}
_SIDE_OUT = {Side.BUY: "Buy", Side.SELL: "Sell"}
_SIDE_IN = {"Buy": Side.BUY, "Sell": Side.SELL}


class BybitExchange:
    """Implements ExchangePort against Bybit v5. See perpsagent.domain.ports.ExchangePort."""

    venue = "bybit"

    def __init__(self, config: dict[str, Any]) -> None:
        self._key = config["api_key"]
        self._secret = config["api_secret"]
        self._testnet = bool(config.get("testnet", True))
        self._category = config.get("category", "linear")
        self._recv_window = str(config.get("recv_window", 5000))
        self._rest = _REST[self._testnet]
        self._ws_url = _WS_PRIVATE[self._testnet]
        self._session = None  # lazy aiohttp.ClientSession

    async def _sess(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "perpsagent-bot/0.1 (+https://perpsagent.example)"}
            )
        return self._session

    # ---- signing ----

    def _sign(self, ts: str, payload: str) -> str:
        pre = ts + self._key + self._recv_window + payload
        return hmac.new(self._secret.encode(), pre.encode(), hashlib.sha256).hexdigest()

    def _headers(self, payload: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": self._sign(ts, payload),
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict[str, Any]) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sess = await self._sess()
        async with sess.get(f"{self._rest}{path}?{qs}", headers=self._headers(qs)) as r:
            return _unwrap(await r.json())

    async def _post(self, path: str, body: dict[str, Any]) -> dict:
        raw = json.dumps(body, separators=(",", ":"))
        sess = await self._sess()
        async with sess.post(f"{self._rest}{path}", data=raw, headers=self._headers(raw)) as r:
            return _unwrap(await r.json())

    # ---- ExchangePort ----

    async def market_meta(self, market: str) -> MarketMeta:
        res = await self._get("/v5/market/instruments-info", {"category": self._category, "symbol": market})
        it = res["list"][0]
        pf, lf = it["priceFilter"], it["lotSizeFilter"]
        return MarketMeta(
            market=market,
            tick_size=Decimal(pf["tickSize"]),
            step_size=Decimal(lf["qtyStep"]),
            min_order_size=Decimal(lf["minOrderQty"]),
        )

    async def best_bid_ask(self, market: str) -> tuple[Decimal, Decimal]:
        res = await self._get("/v5/market/orderbook", {"category": self._category, "symbol": market, "limit": 1})
        return (Decimal(res["b"][0][0]), Decimal(res["a"][0][0]))

    async def place_order(self, order: Order) -> Order:
        res = await self._post(
            "/v5/order/create",
            {
                "category": self._category,
                "symbol": order.market,
                "side": _SIDE_OUT[order.side],
                "orderType": "Limit",
                "qty": str(order.qty),
                "price": str(order.price),
                "timeInForce": "PostOnly" if order.post_only else "GTC",
                "orderLinkId": order.external_id,
            },
        )
        order.order_id = res.get("orderId")
        return order

    async def cancel_order(self, market: str, order_id: str) -> None:
        body = {"category": self._category, "symbol": market}
        body["orderLinkId" if order_id.startswith("grid-") else "orderId"] = order_id
        await self._post("/v5/order/cancel", body)

    async def cancel_all(self, market: str) -> None:
        await self._post("/v5/order/cancel-all", {"category": self._category, "symbol": market})

    async def open_orders(self, market: str) -> Sequence[Order]:
        res = await self._get("/v5/order/realtime", {"category": self._category, "symbol": market})
        out: list[Order] = []
        for o in res.get("list", []):
            try:
                instance_id, level, _ = parse_external_id(o.get("orderLinkId", ""))
            except ValueError:
                continue
            out.append(
                Order(
                    instance_id=instance_id,
                    market=market,
                    side=_SIDE_IN.get(o["side"], Side.BUY),
                    price=Decimal(o["price"]),
                    qty=Decimal(o["qty"]),
                    external_id=o["orderLinkId"],
                    level=level,
                    order_id=o.get("orderId"),
                )
            )
        return out

    async def balance(self) -> BalanceView:
        res = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acct = res["list"][0]
        equity = Decimal(acct.get("totalEquity") or "0")
        avail = Decimal(acct.get("totalAvailableBalance") or "0")
        return BalanceView(equity=equity, available=avail, currency="USDT")

    async def positions(self) -> Sequence[Position]:
        res = await self._get("/v5/position/list", {"category": self._category, "settleCoin": "USDT"})
        out: list[Position] = []
        for p in res.get("list", []):
            size = Decimal(p.get("size") or "0")
            if size == 0:
                continue
            signed = size if p.get("side") == "Buy" else -size
            out.append(Position(market=p["symbol"], size=signed, entry_price=Decimal(p.get("avgPrice") or "0")))
        return out

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Private WS `execution` stream → Fill. Reconnects with exponential backoff.
        Reconcile open orders/positions from REST after each (re)connect."""
        import aiohttp

        backoff = 1
        while True:
            try:
                sess = await self._sess()
                async with sess.ws_connect(self._ws_url, heartbeat=20) as ws:
                    await self._ws_auth(ws)
                    await ws.send_json({"op": "subscribe", "args": ["execution"]})
                    backoff = 1
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("topic") != "execution":
                            continue
                        for e in data.get("data", []):
                            fill = _to_fill(e)
                            if fill is not None:
                                yield fill
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _ws_auth(self, ws) -> None:
        expires = int((time.time() + 10) * 1000)
        sign = hmac.new(self._secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
        await ws.send_json({"op": "auth", "args": [self._key, expires, sign]})

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _unwrap(resp: dict) -> dict:
    if resp.get("retCode", 0) != 0:
        raise RuntimeError(f"bybit error {resp.get('retCode')}: {resp.get('retMsg')}")
    return resp.get("result", {})


def _to_fill(e: dict) -> Fill | None:
    link = e.get("orderLinkId", "")
    try:
        instance_id, level, _ = parse_external_id(link)
    except ValueError:
        return None
    qty = Decimal(e.get("execQty") or "0")
    if qty == 0:
        return None
    return Fill(
        instance_id=instance_id,
        market=e["symbol"],
        side=_SIDE_IN.get(e.get("side"), Side.BUY),
        price=Decimal(e.get("execPrice") or "0"),
        qty=qty,
        external_id=link,
        ts=int(e.get("execTime") or 0),
        level=level,
    )
