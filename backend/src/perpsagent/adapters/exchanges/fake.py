"""In-memory ExchangePort for tests, dry-run, and backtests.

Fully functional: it keeps an order book, simulates limit-order fills as the
price crosses levels, and streams fills like a real venue. This is the seam the
engine and agent run against with zero network or keys.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, AsyncIterator, Sequence

from ...domain.models import BalanceView, Fill, MarketMeta, Order, Position, Side


class FakeExchange:
    venue = "fake"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._mid = Decimal(str(cfg.get("mid", "100")))
        self._tick = Decimal(str(cfg.get("tick", "0.1")))
        self._step = Decimal(str(cfg.get("step", "0.001")))
        self._min = Decimal(str(cfg.get("min_order_size", "0.001")))
        self._equity = Decimal(str(cfg.get("equity", "10000")))
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._fills: asyncio.Queue[Fill] = asyncio.Queue()
        self._seq = 0

    async def market_meta(self, market: str) -> MarketMeta:
        return MarketMeta(market, self._tick, self._step, self._min)

    async def best_bid_ask(self, market: str) -> tuple[Decimal, Decimal]:
        return (self._mid - self._tick, self._mid + self._tick)

    async def place_order(self, order: Order) -> Order:
        self._seq += 1
        order.order_id = f"fake-{self._seq}"
        self._orders[order.external_id] = order
        return order

    async def cancel_order(self, market: str, order_id: str) -> None:
        for ext, o in list(self._orders.items()):
            if o.order_id == order_id or o.external_id == order_id:
                del self._orders[ext]
                return

    async def cancel_all(self, market: str) -> None:
        self._orders = {e: o for e, o in self._orders.items() if o.market != market}

    async def open_orders(self, market: str) -> Sequence[Order]:
        return [o for o in self._orders.values() if o.market == market]

    async def balance(self) -> BalanceView:
        return BalanceView(self._equity, self._equity)

    async def positions(self) -> Sequence[Position]:
        return list(self._positions.values())

    async def stream_fills(self) -> AsyncIterator[Fill]:
        while True:
            yield await self._fills.get()

    # ---- test / simulation helpers (not part of ExchangePort) ----

    async def move_price(self, market: str, price: Decimal) -> list[Fill]:
        """Move the mid; fill any limit order the move crosses, emit Fill events."""
        price = Decimal(str(price))
        self._mid = price
        filled: list[Fill] = []
        for ext, o in sorted(self._orders.items(), key=lambda kv: kv[1].price):
            if o.market != market:
                continue
            crossed = (o.side is Side.BUY and price <= o.price) or (
                o.side is Side.SELL and price >= o.price
            )
            if crossed:
                del self._orders[ext]
                fill = Fill(
                    instance_id=o.instance_id,
                    market=o.market,
                    side=o.side,
                    price=o.price,
                    qty=o.qty,
                    external_id=o.external_id,
                    ts=int(time.time()),
                    level=o.level,
                )
                filled.append(fill)
                await self._fills.put(fill)
        return filled
