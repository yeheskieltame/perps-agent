"""GridEngine — one supervised grid instance over an ExchangePort.

Thin async shell: places the initial grid, then reacts to fills (place the
paired order, accumulate realized PnL). All math is pure (domain/grid.py). Drive
logic from fills, never from order snapshots (mirrors deltaperps).
"""
from __future__ import annotations

import hashlib
from collections import deque
from decimal import Decimal

from ..domain.grid import build_grid_orders, compute_paired_order, plan_levels, quantize
from ..domain.models import EpisodeOutcome, Fill, GridConfig, GridState, Order, Side
from ..domain.pnl import risk_adjusted


class GridEngine:
    def __init__(self, exchange, cfg: GridConfig, store=None) -> None:
        self.ex = exchange
        self.cfg = cfg
        self.store = store
        self.state = GridState.INITIALIZING
        self.levels: list[Decimal] = []
        self.realized = Decimal(0)
        self.fill_count = 0
        self.closes = 0
        self.wins = 0
        self._inv: deque[tuple[Decimal, Decimal]] = deque()  # open BUY inventory (price, qty)
        self._nonce = 0
        self._fills: list[Fill] = []

    async def start(self) -> list[Order]:
        meta = await self.ex.market_meta(self.cfg.market)
        bid, ask = await self.ex.best_bid_ask(self.cfg.market)
        mid = (bid + ask) / 2
        self.levels = [quantize(p, meta.tick_size) for p in plan_levels(self.cfg)]
        orders = build_grid_orders(self.cfg, self.levels, mid)
        for o in orders:
            await self.ex.place_order(o)
        self.state = GridState.RUNNING
        return orders

    async def handle_fill(self, fill: Fill) -> Order | None:
        if fill.instance_id != self.cfg.instance_id:
            return None
        if self.store is not None:
            await self.store.record_fill(fill)  # persist BEFORE placing the pair
        self._fills.append(fill)
        self.fill_count += 1
        self._match_pnl(fill)
        self._nonce += 1
        paired = compute_paired_order(self.cfg, self.levels, fill.level, fill.side, self._nonce)
        if paired is not None:
            await self.ex.place_order(paired)
        return paired

    def _match_pnl(self, fill: Fill) -> None:
        if fill.side is Side.BUY:
            self._inv.append((fill.price, fill.qty))
            return
        qty = fill.qty
        while qty > 0 and self._inv:
            bprice, bqty = self._inv[0]
            matched = min(qty, bqty)
            pnl = (fill.price - bprice) * matched
            self.realized += pnl
            self.closes += 1
            if pnl > 0:
                self.wins += 1
            qty -= matched
            if matched >= bqty:
                self._inv.popleft()
            else:
                self._inv[0] = (bprice, bqty - matched)

    def rehydrate(self, fills) -> None:
        """Replay persisted fills to restore realized PnL / winrate / inventory after
        a restart. Does not place orders (assumes venue orders are reconciled)."""
        for f in fills:
            self._fills.append(f)
            self.fill_count += 1
            self._match_pnl(f)
        self.state = GridState.RUNNING

    async def consume(self) -> None:
        """Background loop: react to streamed fills until stopped."""
        async for fill in self.ex.stream_fills():
            if self.state is not GridState.RUNNING:
                break
            await self.handle_fill(fill)

    async def run(self) -> None:
        await self.start()
        await self.consume()

    def pause(self) -> None:
        if self.state is GridState.RUNNING:
            self.state = GridState.HALTED

    async def stop(self) -> None:
        self.state = GridState.EXITING
        await self.ex.cancel_all(self.cfg.market)
        self.state = GridState.HALTED

    @property
    def winrate(self) -> float:
        return (self.wins / self.closes) if self.closes else 0.0

    def fills_merkle_root(self) -> str:
        h = hashlib.sha256()
        for f in self._fills:
            h.update(f"{f.external_id}:{f.side.value}:{f.price}:{f.qty}".encode())
        return "0x" + h.hexdigest()

    def outcome(self) -> EpisodeOutcome:
        return EpisodeOutcome(
            instance_id=self.cfg.instance_id,
            realized_pnl=self.realized,
            winrate=self.winrate,
            max_adverse_excursion=0.0,  # TODO: track equity curve for MAE
            fill_count=self.fill_count,
            fills_merkle_root=self.fills_merkle_root(),
            risk_adjusted=risk_adjusted(self.realized, 0.0),
        )
