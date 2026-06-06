"""GridEngine — one supervised grid instance over an ExchangePort.

Thin async shell: enforces leverage, places the initial grid, then reacts to
fills (place the paired order, accumulate realized PnL). A background `monitor`
loop re-centers the grid to FOLLOW price when it leaves the band (so a trending
market doesn't strand the grid as a one-sided bag) and enforces the circuit
breaker. All math is pure (domain/grid.py). Drive logic from fills, never from
order snapshots (mirrors deltaperps).
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from decimal import Decimal

from ..domain.grid import build_grid_orders, compute_paired_order, levels_for, plan_levels, quantize
from ..domain.models import EpisodeOutcome, Fill, GridConfig, GridState, Order, Side
from ..domain.pnl import risk_adjusted
from .safety import CircuitBreaker


class GridEngine:
    def __init__(self, exchange, cfg: GridConfig, store=None,
                 breaker: CircuitBreaker | None = None, monitor_interval: float = 0.0) -> None:
        self.ex = exchange
        self.cfg = cfg
        self.store = store
        self.breaker = breaker
        self.monitor_interval = monitor_interval  # seconds; <=0 disables re-center/monitor
        self.state = GridState.INITIALIZING
        self.levels: list[Decimal] = []
        self.realized = Decimal(0)
        self.fill_count = 0
        self.closes = 0
        self.wins = 0
        self._inv: deque[tuple[Decimal, Decimal]] = deque()  # open BUY inventory (price, qty)
        self._nonce = 0
        self._fills: list[Fill] = []
        # re-center state (live band; cfg stays the committed/original band)
        self._gen = 0
        self.tick = Decimal(0)
        self.center = Decimal(0)
        self.lower = cfg.lower
        self.upper = cfg.upper
        self._lower_ratio = Decimal(1)
        self._upper_ratio = Decimal(1)
        self.halt_reason = ""

    async def start(self) -> list[Order]:
        meta = await self.ex.market_meta(self.cfg.market)
        self.tick = meta.tick_size
        try:
            await self.ex.set_leverage(self.cfg.market, self.cfg.leverage)  # enforce user choice
        except Exception as e:  # noqa: BLE001 — leverage is best-effort, never block trading
            print(f"  ! set_leverage({self.cfg.leverage}x) failed: {e}")
        bid, ask = await self.ex.best_bid_ask(self.cfg.market)
        mid = (bid + ask) / 2
        self.center = mid
        self._lower_ratio = self.cfg.lower / mid  # preserve exact band shape on re-center
        self._upper_ratio = self.cfg.upper / mid
        self.lower, self.upper = self.cfg.lower, self.cfg.upper
        self.levels = [quantize(p, self.tick) for p in plan_levels(self.cfg)]
        orders = build_grid_orders(self.cfg, self.levels, mid, self._gen)
        for o in orders:
            await self.ex.place_order(o)
        self.state = GridState.RUNNING
        return orders

    # ---- live adaptation: re-center to follow price ----

    async def maybe_recenter(self) -> bool:
        """One supervisory check: enforce the breaker on unrealized PnL, then
        re-center the grid if price has left the band. Returns True if re-centered.
        Called every `monitor_interval`s by `monitor` (and directly in tests)."""
        if self.state is not GridState.RUNNING:
            return False
        bid, ask = await self.ex.best_bid_ask(self.cfg.market)
        mid = (bid + ask) / 2
        await self._check_safety(mid)
        if self.state is not GridState.RUNNING:
            return False
        if self.lower <= mid <= self.upper:
            return False
        await self._recenter(mid)
        return True

    async def _recenter(self, new_mid: Decimal) -> None:
        """Cancel the stale grid and re-lay it around `new_mid`, same band shape.
        Inventory and realized PnL carry over; a fresh generation namespaces the
        new external_ids so they never collide with already-seen orderLinkIds."""
        self.state = GridState.REBALANCING
        await self.ex.cancel_all(self.cfg.market)
        self.center = new_mid
        self.lower = new_mid * self._lower_ratio
        self.upper = new_mid * self._upper_ratio
        self.levels = [quantize(p, self.tick)
                       for p in levels_for(self.lower, self.upper, self.cfg.levels, self.cfg.spacing)]
        self._gen += 1
        for o in build_grid_orders(self.cfg, self.levels, new_mid, self._gen):
            await self.ex.place_order(o)
        self.state = GridState.RUNNING
        print(f"  ~ recenter -> mid {new_mid} band [{self.lower}, {self.upper}] gen {self._gen}")

    async def monitor(self) -> None:
        """Background loop: re-center on band-exit and enforce risk caps until stop."""
        if self.monitor_interval <= 0:
            return
        while self.state in (GridState.RUNNING, GridState.REBALANCING):
            await asyncio.sleep(self.monitor_interval)
            try:
                await self.maybe_recenter()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
                print(f"  ! monitor tick failed: {e}")

    # ---- risk ----

    def net_inventory(self) -> Decimal:
        """Open long base qty (BUYs not yet matched by a SELL)."""
        return sum((q for _, q in self._inv), Decimal(0))

    def unrealized(self, mark: Decimal) -> Decimal:
        return sum(((mark - bp) * bq for bp, bq in self._inv), Decimal(0))

    async def _check_safety(self, mark: Decimal | None = None) -> None:
        if self.breaker is None or self.state is not GridState.RUNNING:
            return
        pnl = self.realized + (self.unrealized(mark) if mark is not None else Decimal(0))
        reason = self.breaker.check(self.net_inventory(), pnl)
        if reason:
            await self._halt(reason)

    async def _halt(self, reason: str) -> None:
        if self.state is GridState.HALTED:
            return
        self.state = GridState.HALTED
        self.halt_reason = reason
        print(f"  !! circuit breaker tripped: {reason} — cancel_all + flatten")
        try:
            await self.ex.cancel_all(self.cfg.market)
        except Exception as e:  # noqa: BLE001
            print(f"    cancel_all failed: {e}")
        try:
            await self.ex.flatten(self.cfg.market)
        except Exception as e:  # noqa: BLE001
            print(f"    flatten failed: {e}")

    async def handle_fill(self, fill: Fill) -> Order | None:
        if fill.instance_id != self.cfg.instance_id:
            return None
        if self.store is not None:
            await self.store.record_fill(fill)  # persist BEFORE placing the pair
        self._fills.append(fill)
        self.fill_count += 1
        self._match_pnl(fill)
        await self._check_safety(fill.price)  # risk cap before placing more orders
        if self.state is not GridState.RUNNING:
            return None
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
            if self.state not in (GridState.RUNNING, GridState.REBALANCING):
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
