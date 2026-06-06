"""GridEngine — one supervised grid instance over an ExchangePort.

Thin async shell: enforces leverage, places the initial grid, then reacts to
fills (place the paired order, accumulate realized PnL). A background `monitor`
loop re-centers the grid to FOLLOW price when it leaves the band (so a trending
market doesn't strand the grid as a one-sided bag) and enforces two guards:
  - CircuitBreaker caps the downside (inventory / drawdown).
  - ProfitGuard locks the upside (take-profit / trailing-stop) — rides a move,
    then banks when it turns, instead of giving the unrealized gain back.
Re-center is inventory-aware: it never averages UP (no new BUY above the average
long entry), so a pump can't keep stacking the bag. All math is pure
(domain/grid.py). Drive logic from fills, never from order snapshots.
"""
from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal

from ..domain.grid import (
    _external_id, build_grid_orders, compute_paired_order, levels_for, plan_levels, quantize,
)
from ..domain.models import EpisodeOutcome, Fill, GridConfig, GridState, Order, Side
from ..domain.pnl import risk_adjusted
from .safety import CircuitBreaker, ProfitGuard


class GridEngine:
    def __init__(self, exchange, cfg: GridConfig, store=None, breaker: CircuitBreaker | None = None,
                 monitor_interval: float = 0.0, profit_guard: ProfitGuard | None = None) -> None:
        self.ex = exchange
        self.cfg = cfg
        self.store = store
        self.breaker = breaker
        self.profit_guard = profit_guard
        self.monitor_interval = monitor_interval  # seconds; <=0 disables re-center/monitor
        self.state = GridState.INITIALIZING
        self.levels: list[Decimal] = []
        self.realized = Decimal(0)
        self.fill_count = 0
        self.closes = 0
        self.wins = 0
        self.pos_qty = Decimal(0)  # SIGNED net position (>0 long, <0 short) — mirrors the venue
        self.pos_avg = Decimal(0)  # average entry of the current position (0 when flat)
        self._nonce = 0
        self._oid = 0  # global monotonic order-id counter — every order id is unique forever
        self._fills: list[Fill] = []
        # re-center state (live band; cfg stays the committed/original band)
        self._gen = 0
        self.tick = Decimal(0)
        self.center = Decimal(0)
        self.lower = cfg.lower
        self.upper = cfg.upper
        self._lower_ratio = Decimal(1)
        self._upper_ratio = Decimal(1)
        self.exit_reason = ""
        self.exit_kind = ""

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
            await self._place(o)
        self.state = GridState.RUNNING
        return orders

    async def _place(self, order: Order) -> None:
        """Place an order with a globally-unique external_id. Venues reject a
        reused orderLinkId even after cancel (Bybit err 110072), so a fresh id is
        stamped on every placement. One rejected order is logged, never fatal —
        it must not orphan the grid."""
        self._oid += 1
        order.external_id = _external_id(self.cfg.instance_id, order.level, self._oid)
        try:
            await self.ex.place_order(order)
        except Exception as e:  # noqa: BLE001
            print(f"  ! place {order.side.value} L{order.level} @ {order.price} failed: {e}")

    # ---- live adaptation: re-center to follow price ----

    async def maybe_recenter(self) -> bool:
        """One supervisory check: enforce risk/profit guards on unrealized PnL, then
        re-center the grid if price has left the band. Returns True if re-centered.
        Called every `monitor_interval`s by `monitor` (and directly in tests)."""
        if self.state is not GridState.RUNNING:
            return False
        bid, ask = await self.ex.best_bid_ask(self.cfg.market)
        mid = (bid + ask) / 2
        await self._check_guards(mid)
        if self.state is not GridState.RUNNING:
            return False
        if self.lower <= mid <= self.upper:
            return False
        await self._recenter(mid)
        return True

    async def _recenter(self, new_mid: Decimal) -> None:
        """Cancel the stale grid and re-lay it around `new_mid`, same band shape.
        Inventory-aware: when net long, never place a BUY above the average entry
        (no averaging up into a pump). Inventory + realized PnL carry over; a fresh
        generation namespaces the new external_ids so they never collide."""
        self.state = GridState.REBALANCING
        try:
            await self.ex.cancel_all(self.cfg.market)
            self.center = new_mid
            self.lower = new_mid * self._lower_ratio
            self.upper = new_mid * self._upper_ratio
            self.levels = [quantize(p, self.tick)
                           for p in levels_for(self.lower, self.upper, self.cfg.levels, self.cfg.spacing)]
            self._gen += 1
            orders = build_grid_orders(self.cfg, self.levels, new_mid, self._gen)
            net = self.net_inventory()
            if net > 0:    # long: don't average UP (no BUY above the entry)
                orders = [o for o in orders if not (o.side is Side.BUY and o.price > self.pos_avg)]
            elif net < 0:  # short: don't average DOWN (no SELL below the entry)
                orders = [o for o in orders if not (o.side is Side.SELL and o.price < self.pos_avg)]
            for o in orders:
                if self.state is GridState.HALTED:  # a guard fired mid-rebuild — stop placing
                    break
                await self._place(o)
            print(f"  ~ recenter -> mid {new_mid} band [{self.lower}, {self.upper}] gen {self._gen} "
                  f"({len(orders)} orders, net_inv {net})")
        finally:
            if self.state is GridState.REBALANCING:  # ALWAYS leave a runnable state (never orphan)
                self.state = GridState.RUNNING

    async def monitor(self) -> None:
        """Background loop: re-center on band-exit and enforce guards until stop."""
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

    # ---- risk + profit ----

    def net_inventory(self) -> Decimal:
        """SIGNED net position (>0 long, <0 short) — mirrors the venue."""
        return self.pos_qty

    def unrealized(self, mark: Decimal) -> Decimal:
        return (mark - self.pos_avg) * self.pos_qty  # correct for both long and short

    def _avg_entry(self) -> Decimal:
        return self.pos_avg

    async def _check_guards(self, mark: Decimal | None = None) -> None:
        if self.state not in (GridState.RUNNING, GridState.REBALANCING):  # stay armed during a re-center
            return
        pnl = self.realized + (self.unrealized(mark) if mark is not None else Decimal(0))
        if self.breaker is not None:
            reason = self.breaker.check(self.net_inventory(), pnl)
            if reason:
                await self._exit(reason, "circuit-breaker")
                return
        if self.profit_guard is not None and mark is not None:
            reason = self.profit_guard.check(pnl)
            if reason:
                await self._exit(reason, "profit-lock")

    async def _exit(self, reason: str, kind: str) -> None:
        """Emergency/profit exit: stop the grid, cancel everything, flatten."""
        if self.state is GridState.HALTED:
            return
        self.state = GridState.HALTED
        self.exit_reason = reason
        self.exit_kind = kind
        print(f"  !! {kind}: {reason} — cancel_all + flatten")
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
        self._apply_fill(fill.side, fill.price, fill.qty)
        await self._check_guards(fill.price)  # risk + profit guards before placing more orders
        if self.state is not GridState.RUNNING:
            return None
        self._nonce += 1
        paired = compute_paired_order(self.cfg, self.levels, fill.level, fill.side, self._nonce)
        if paired is not None:
            await self._place(paired)
        return paired

    def _apply_fill(self, side: Side, price: Decimal, qty: Decimal) -> None:
        """Signed-position accounting (mirrors a perp account): average in when a
        fill extends the position, realize PnL when it reduces it, and re-base the
        average when it flips. A SELL from flat opens a SHORT (not ignored)."""
        signed = qty if side is Side.BUY else -qty
        pos, avg = self.pos_qty, self.pos_avg
        if pos == 0 or (pos > 0) == (signed > 0):
            new_qty = pos + signed                      # opening / adding same side
            self.pos_avg = (avg * abs(pos) + price * qty) / abs(new_qty)
            self.pos_qty = new_qty
            return
        closed = min(qty, abs(pos))                     # opposite side -> close (maybe flip)
        pnl = (price - avg) * closed if pos > 0 else (avg - price) * closed
        self.realized += pnl
        self.closes += 1
        if pnl > 0:
            self.wins += 1
        new_qty = pos + signed
        self.pos_qty = new_qty
        if new_qty == 0:
            self.pos_avg = Decimal(0)
        elif (new_qty > 0) != (pos > 0):
            self.pos_avg = price                        # flipped: remainder opens at this fill

    def rehydrate(self, fills) -> None:
        """Replay persisted fills to restore realized PnL / winrate / inventory after
        a restart. Does not place orders (assumes venue orders are reconciled)."""
        for f in fills:
            self._fills.append(f)
            self.fill_count += 1
            self._apply_fill(f.side, f.price, f.qty)
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
