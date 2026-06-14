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
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Sequence

from ....domain.grid import parse_external_id
from ....domain.models import BalanceView, Fill, MarketMeta, Order, Position, Side
from .throttle import RateLimiter, is_retryable

_BATCH_MAX = 20  # Bybit v5 create-batch cap (orders per request)

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
        # Client-side pacing + retry: a single account shared by many grids will
        # trip Bybit's per-UID/IP caps without this. rate_limit<=0 disables.
        rate = float(config.get("rate_limit", 10.0))  # req/s, global per account
        self._limiter = RateLimiter(rate, burst=max(rate, 1.0) * 2)
        self._max_retries = int(config.get("max_retries", 3))
        self._retry_base = float(config.get("retry_base", 0.5))   # backoff seconds
        self._retry_cap = float(config.get("retry_cap", 8.0))
        # Fill-stream gap recovery: watermark of the newest execTime yielded (ms)
        # + a bounded execId memory so REST backfill and WS redelivery never
        # double-count a fill.
        self._last_exec_ms = 0
        self._seen_execs: OrderedDict[str, None] = OrderedDict()

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
        return _unwrap(await self._request("GET", path, qs))

    async def klines(self, market: str, interval: str = "1", limit: int = 60) -> list[Decimal]:
        """Close prices, oldest -> newest (public endpoint). Feeds the local
        regime fallback (agent/sense.py) for markets the external signal
        providers don't cover."""
        res = await self._get("/v5/market/kline", {"category": self._category, "symbol": market,
                                                   "interval": interval, "limit": str(limit)})
        return [Decimal(row[4]) for row in reversed(res["list"])]

    async def _post(self, path: str, body: dict[str, Any]) -> dict:
        raw = json.dumps(body, separators=(",", ":"))
        return _unwrap(await self._request("POST", path, raw))

    async def _request(self, method: str, path: str, payload: str) -> dict:
        """One signed Bybit call, paced by the rate limiter and retried with
        exponential backoff on transient errors (HTTP 429/5xx, retCode
        10002/10006/10016/10018). Returns the FULL envelope (callers unwrap).
        Re-signs every attempt so a backoff never sends an expired timestamp."""
        import aiohttp

        await self._limiter.acquire()
        delay = self._retry_base
        attempt = 0
        while True:
            headers = self._headers(payload)
            sess = await self._sess()
            try:
                if method == "GET":
                    url = f"{self._rest}{path}?{payload}" if payload else f"{self._rest}{path}"
                    async with sess.get(url, headers=headers) as r:
                        status, resp = r.status, await r.json()
                else:
                    async with sess.post(f"{self._rest}{path}", data=payload, headers=headers) as r:
                        status, resp = r.status, await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_cap)
                attempt += 1
                continue
            if is_retryable(status, int(resp.get("retCode", 0) or 0)) and attempt < self._max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_cap)
                attempt += 1
                continue
            return resp

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

    async def set_leverage(self, market: str, leverage: Decimal) -> None:
        """v5 set-leverage. Bybit returns retCode 110043 when leverage is already
        at this value — treat that as success."""
        try:
            await self._post(
                "/v5/position/set-leverage",
                {"category": self._category, "symbol": market,
                 "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
            )
        except RuntimeError as e:
            if "110043" not in str(e):  # "leverage not modified"
                raise

    def _order_payload(self, order: Order) -> dict[str, Any]:
        """The per-order fields shared by single create and batch create (the batch
        endpoint takes `category` once at the top level, so it is not included here)."""
        return {
            "symbol": order.market,
            "side": _SIDE_OUT[order.side],
            "orderType": "Limit",
            "qty": str(order.qty),
            "price": str(order.price),
            "timeInForce": "PostOnly" if order.post_only else "GTC",
            "orderLinkId": order.external_id,
        }

    async def place_order(self, order: Order) -> Order:
        res = await self._post("/v5/order/create", {"category": self._category, **self._order_payload(order)})
        order.order_id = res.get("orderId")
        return order

    async def place_orders(self, orders: Sequence[Order]) -> list[Order]:
        """Batch-create up to 20 orders per request (/v5/order/create-batch) — cuts
        one-account REST pressure ~20x vs. single creates, so a re-center (cancel +
        re-lay the whole grid) is 1-2 round-trips instead of N. Partial failures are
        logged per order (retExtInfo), never fatal: a holed grid beats a crash."""
        placed: list[Order] = []
        for chunk in _chunked(list(orders), _BATCH_MAX):
            body = {"category": self._category, "request": [self._order_payload(o) for o in chunk]}
            resp = await self._request("POST", "/v5/order/create-batch", json.dumps(body, separators=(",", ":")))
            if int(resp.get("retCode", 0) or 0) != 0:  # whole batch rejected
                print(f"  ! batch create failed: {resp.get('retCode')} {resp.get('retMsg')}")
                placed.extend(chunk)  # order_id left unset; caller tolerates a hole
                continue
            rows = (resp.get("result") or {}).get("list") or []
            codes = (resp.get("retExtInfo") or {}).get("list") or []
            for i, o in enumerate(chunk):
                o.order_id = (rows[i].get("orderId") if i < len(rows) else None)
                code = codes[i].get("code") if i < len(codes) and isinstance(codes[i], dict) else 0
                if code not in (0, None):
                    print(f"  ! batch place L{o.level} @ {o.price} rejected: {code} {codes[i].get('msg')}")
                placed.append(o)
        return placed

    async def cancel_order(self, market: str, order_id: str) -> None:
        body = {"category": self._category, "symbol": market}
        body["orderLinkId" if order_id.startswith("grid-") else "orderId"] = order_id
        await self._post("/v5/order/cancel", body)

    async def cancel_orders(self, market: str, orders: Sequence[Order]) -> None:
        """Batch-cancel up to 20 orders per request (/v5/order/cancel-batch) — the
        cancel half of an instance-scoped re-center in 1-2 round-trips. Partial
        failures (e.g. an order that just filled) are logged, never fatal."""
        for chunk in _chunked(list(orders), _BATCH_MAX):
            body = {"category": self._category,
                    "request": [{"symbol": market, "orderLinkId": o.external_id} for o in chunk]}
            resp = await self._request("POST", "/v5/order/cancel-batch", json.dumps(body, separators=(",", ":")))
            if int(resp.get("retCode", 0) or 0) != 0:  # whole batch rejected
                print(f"  ! batch cancel failed: {resp.get('retCode')} {resp.get('retMsg')}")
                continue
            codes = (resp.get("retExtInfo") or {}).get("list") or []
            for i, o in enumerate(chunk):
                code = codes[i].get("code") if i < len(codes) and isinstance(codes[i], dict) else 0
                if code not in (0, None):
                    print(f"  ! batch cancel {o.external_id} rejected: {code} {codes[i].get('msg')}")

    async def cancel_all(self, market: str) -> None:
        await self._post("/v5/order/cancel-all", {"category": self._category, "symbol": market})

    async def flatten(self, market: str) -> None:
        """Emergency exit: market-close any open position on `market` (reduce-only)."""
        res = await self._get("/v5/position/list", {"category": self._category, "settleCoin": "USDT"})
        for p in res.get("list", []):
            if p.get("symbol") != market:
                continue
            size = Decimal(p.get("size") or "0")
            if size == 0:
                continue
            close_side = "Sell" if p.get("side") == "Buy" else "Buy"
            await self._post(
                "/v5/order/create",
                {"category": self._category, "symbol": market, "side": close_side,
                 "orderType": "Market", "qty": str(size), "reduceOnly": True},
            )

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
        """USDT margin on a UNIFIED account. `available` is the free margin a new grid
        may commit: the USDT coin's availableToWithdraw, falling back to the account
        figure, then the USDT wallet balance (testnet UNIFIED often returns
        availableToWithdraw as "") — never the cross-coin totalEquity, which would
        over-size against other coins and unrealized PnL."""
        res = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acct = res["list"][0]
        usdt = next((c for c in acct.get("coin", []) if c.get("coin") == "USDT"), {})
        wallet = _dec(usdt.get("walletBalance"))
        available = (_dec(usdt.get("availableToWithdraw"))
                     or _dec(acct.get("totalAvailableBalance")) or wallet)
        equity = _dec(usdt.get("equity")) or wallet or _dec(acct.get("totalEquity"))
        return BalanceView(equity=equity, available=available, currency="USDT")

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
        """Private WS `execution` stream → Fill. Reconnects with exponential
        backoff; after every reconnect, executions that happened while the socket
        was down are backfilled from REST (/v5/execution/list) and deduped by
        execId — a WS gap never loses a fill, so engine inventory/PnL (and the
        circuit breaker reading them) stay true to the venue."""
        import aiohttp

        backoff = 1
        while True:
            try:
                sess = await self._sess()
                async with sess.ws_connect(self._ws_url, heartbeat=20) as ws:
                    await self._ws_auth(ws)
                    await ws.send_json({"op": "subscribe", "args": ["execution"]})
                    backoff = 1
                    for fill in await self._backfill():
                        yield fill
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("topic") != "execution":
                            continue
                        for e in data.get("data", []):
                            if self._dedup(e.get("execId", "")):
                                continue
                            fill = _to_fill(e)
                            if fill is not None:
                                self._last_exec_ms = max(self._last_exec_ms, fill.ts)
                                yield fill
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _dedup(self, exec_id: str) -> bool:
        """True if this execId was already yielded (WS redelivery or the overlap
        between live stream and REST backfill). Bounded memory: ~4096 newest ids."""
        if not exec_id:
            return False
        if exec_id in self._seen_execs:
            return True
        self._seen_execs[exec_id] = None
        if len(self._seen_execs) > 4096:
            self._seen_execs.popitem(last=False)
        return False

    async def _backfill(self) -> list[Fill]:
        """Fills missed while the WS was down. First connect: nothing to recover,
        just start the watermark. A backfill failure is logged — it must not tear
        down the connection that was just re-established."""
        if self._last_exec_ms == 0:
            self._last_exec_ms = int(time.time() * 1000)
            return []
        try:
            # 5s overlap absorbs local↔server clock skew; _dedup eats the overlap.
            missed = await self._missed_fills(max(1, self._last_exec_ms - 5_000))
        except Exception as e:  # noqa: BLE001
            print(f"  ! fill backfill failed: {e}")
            return []
        for f in missed:
            self._last_exec_ms = max(self._last_exec_ms, f.ts)
        if missed:
            print(f"  ~ backfilled {len(missed)} fill(s) missed during a WS gap")
        return missed

    async def _missed_fills(self, since_ms: int) -> list[Fill]:
        """Executions since `since_ms`, oldest-first. /v5/execution/list returns
        newest-first, so a full page (100) means older rows remain — page strictly
        backwards by endTime until the window is covered. Deduped by execId."""
        out: list[Fill] = []
        end_ms = 0  # 0 = open-ended (now)
        while True:
            params: dict[str, Any] = {"category": self._category, "startTime": since_ms, "limit": 100}
            if end_ms:
                params["endTime"] = end_ms
            res = await self._get("/v5/execution/list", params)
            rows = res.get("list") or []
            for e in rows:
                if self._dedup(e.get("execId", "")):
                    continue
                fill = _to_fill(e)
                if fill is not None:
                    out.append(fill)
            if len(rows) < 100:
                break
            end_ms = min(int(r.get("execTime") or 0) for r in rows) - 1
            if end_ms <= since_ms:
                break
        out.sort(key=lambda f: f.ts)
        return out

    async def _ws_auth(self, ws) -> None:
        expires = int((time.time() + 10) * 1000)
        sign = hmac.new(self._secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
        await ws.send_json({"op": "auth", "args": [self._key, expires, sign]})

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _dec(raw: Any) -> Decimal:
    """Parse a Bybit numeric field; "" / None / missing → 0 (UNIFIED omits fields)."""
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError):
        return Decimal(0)


def _chunked(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


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
