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
    _external_id, build_grid_orders, compute_paired_order, levels_for, plan_levels,
    quantize, quantize_qty,
)
from ..domain.models import EpisodeOutcome, Fill, GridConfig, GridState, Order, Side
from ..domain.pnl import risk_adjusted
from .safety import AccountGuard, CircuitBreaker, ProfitGuard


class GridEngine:
    _exit_retry_delay = 1.0  # backoff base for emergency-exit retries (tests set 0)
    # Thesis-break exit (live 2026-06-11): a slow grind pins the grid at the
    # inventory cap and waiting for the drawdown breaker donates the whole gap
    # (-2.5 instead of -1.2). When inventory sits at the cap AND the recent price
    # path is one-directional against it, the mean-reversion thesis is broken —
    # exit early. ER = |net move| / path length over the venue's kline window.
    THESIS_ER = 0.3        # signed efficiency ratio that counts as "directional"
    THESIS_CAP_FRAC = 0.9  # "at the cap" = within 90% (partials can stop short)

    ACCOUNT_CHECK_EVERY = 4  # monitor ticks between equity reads (~1/min at 15s ticks)

    def __init__(self, exchange, cfg: GridConfig, store=None, breaker: CircuitBreaker | None = None,
                 monitor_interval: float = 0.0, profit_guard: ProfitGuard | None = None,
                 bias_fn=None, account_guard: AccountGuard | None = None,
                 timeframe: str = "1") -> None:
        self.ex = exchange
        self.cfg = cfg
        self.store = store
        self.breaker = breaker
        self.profit_guard = profit_guard
        self.account_guard = account_guard
        # The user's operating timeframe: thesis-break reads its efficiency-ratio
        # window on these bars, so "directional against us" means directional on
        # the structure the user chose to trade — not on 1m noise.
        self.timeframe = timeframe
        self._guard_tick = 0  # throttles the account-equity read in the monitor
        self.monitor_interval = monitor_interval  # seconds; <=0 disables re-center/monitor
        # Grid mode (see domain/grid.py build_grid_orders): starts at the committed
        # cfg.bias; an optional async `bias_fn(current_bias) -> int` re-evaluates the
        # regime on every re-center, so one episode can flow ranging -> trending.
        self.bias = cfg.bias
        self.bias_fn = bias_fn
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
        # Resting orders THIS engine placed (issue #36): the inventory cap must hold
        # at placement time too — a fast sweep through every resting order may not
        # take |position| past the cap. Overcounts on partial fills/rejects, which
        # only errs toward placing less (safe).
        self._resting: dict[str, Order] = {}
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
        # Align the order size to the venue's qty step + min, or every order is
        # rejected (an unaligned auto-size → RUNNING grid with zero resting orders).
        self.cfg.order_size = quantize_qty(self.cfg.order_size, meta.step_size, meta.min_order_size)
        try:
            await self.ex.set_leverage(self.cfg.market, self.cfg.leverage)  # enforce user choice
        except Exception as e:  # noqa: BLE001 — leverage is best-effort, never block trading
            print(f"  ! set_leverage({self.cfg.leverage}x) failed: {e}")
        if self.account_guard is not None and self.account_guard.max_drop > 0:
            try:
                self.account_guard.arm((await self.ex.balance()).equity)
            except Exception as e:  # noqa: BLE001 — unarmed guard fails open; breaker still caps the episode
                print(f"  ! account guard could not read start equity ({e}) — guard inactive")
        bid, ask = await self.ex.best_bid_ask(self.cfg.market)
        mid = (bid + ask) / 2
        self.center = mid
        self._lower_ratio = self.cfg.lower / mid  # preserve exact band shape on re-center
        self._upper_ratio = self.cfg.upper / mid
        self.lower, self.upper = self.cfg.lower, self.cfg.upper
        self.levels = [quantize(p, self.tick) for p in plan_levels(self.cfg)]
        orders = build_grid_orders(self.cfg, self.levels, mid, self._gen, bias=self.bias)
        await self._place_many(orders)
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
            self._resting[order.external_id] = order
        except Exception as e:  # noqa: BLE001
            print(f"  ! place {order.side.value} L{order.level} @ {order.price} failed: {e}")

    async def _place_many(self, orders: list[Order]) -> None:
        """Stamp globally-unique external_ids on a whole batch, then place it. Uses
        the venue's batch endpoint when it exposes one (`place_orders`) — one
        round-trip per ~20 orders instead of N, which is what lets a re-center keep
        up under one shared account — else places one-by-one. Tolerant of partial
        failure: a rejected order is logged, never orphans the grid."""
        for o in orders:
            self._oid += 1
            o.external_id = _external_id(self.cfg.instance_id, o.level, self._oid)
        batch = getattr(self.ex, "place_orders", None)
        if batch is None:  # venue has no batch endpoint — place individually
            for o in orders:
                if self.state is GridState.HALTED:  # a guard fired mid-place — stop
                    break
                try:
                    await self.ex.place_order(o)
                    self._resting[o.external_id] = o
                except Exception as e:  # noqa: BLE001
                    print(f"  ! place {o.side.value} L{o.level} @ {o.price} failed: {e}")
            return
        if self.state is GridState.HALTED:  # a guard fired — don't place into a halted grid
            return
        try:
            placed = await batch(orders)
            accepted = [o for o in (placed or orders) if getattr(o, "order_id", None)]
            for o in accepted:  # only track what the venue actually accepted
                self._resting[o.external_id] = o
            if orders and not accepted:  # whole batch rejected — make it loud, not silent
                print(f"  ! grid {self.cfg.instance_id}: venue accepted 0/{len(orders)} orders "
                      f"(size={self.cfg.order_size}) — check qty step / min order value / margin")
        except Exception as e:  # noqa: BLE001
            print(f"  ! batch place ({len(orders)} orders) failed: {e}")

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
        if await self._account_break(mid):
            return False
        if await self._thesis_break():
            return False
        if self.lower <= mid <= self.upper:
            return False
        await self._recenter(mid)
        return True

    async def _account_break(self, mark: Decimal) -> bool:
        """Account-level kill-switch: wallet equity (ALL markets, all episodes)
        dropped past the guard's cap -> exit this grid. Equity is read every
        ACCOUNT_CHECK_EVERY ticks to keep the monitor light on the venue API."""
        if self.account_guard is None or self.account_guard.max_drop <= 0:
            return False
        self._guard_tick += 1
        if self._guard_tick % self.ACCOUNT_CHECK_EVERY != 1:
            return False
        try:
            equity = (await self.ex.balance()).equity
        except Exception:  # noqa: BLE001 — a failed read must not kill the monitor
            return False
        reason = self.account_guard.check(equity)
        if reason:
            await self._exit(reason, "account-guard", mark)
            return True
        return False

    async def _thesis_break(self) -> bool:
        """Exit early when capped inventory faces a one-directional grind (see the
        THESIS_* constants). Venues without klines skip the check — the breaker
        remains the backstop."""
        if self.breaker is None or self.breaker.max_inventory <= 0:
            return False
        net = self.net_inventory()
        if abs(net) < self.breaker.max_inventory * Decimal(str(self.THESIS_CAP_FRAC)):
            return False
        kl = getattr(self.ex, "klines", None)
        if kl is None:
            return False
        try:
            try:
                closes = [float(c) for c in await kl(self.cfg.market, self.timeframe, 60)]
            except TypeError:  # ports with a (market)-only signature
                closes = [float(c) for c in await kl(self.cfg.market)]
        except Exception:  # noqa: BLE001 — a dead feed must not kill the monitor
            return False
        if len(closes) < 10:
            return False
        # Two windows: the full read AND its most recent half. A fast dump lives
        # inside the half-window while the full window still remembers the rally
        # that preceded it (live 2026-06-12, LAB ep 2: a 3.5%/30min dump diluted
        # to ER < 0.3 over the hour — the breaker paid the difference).
        for window in (closes, closes[len(closes) // 2:]):
            path = sum(abs(b - a) for a, b in zip(window, window[1:]))
            if path == 0:
                continue
            er = (window[-1] - window[0]) / path  # signed: +1 straight up, -1 straight down
            if (net > 0 and er <= -self.THESIS_ER) or (net < 0 and er >= self.THESIS_ER):
                await self._exit(f"thesis break: efficiency {er:+.2f} over {len(window)} bars "
                                 f"against {net} inventory at cap", "thesis-break",
                                 Decimal(str(window[-1])))
                return True
        return False

    async def _recenter(self, new_mid: Decimal) -> None:
        """Cancel the stale grid and re-lay it around `new_mid`, same band shape.
        Inventory-aware: when net long, never place a BUY above the average entry
        (no averaging up into a pump). Inventory + realized PnL carry over; a fresh
        generation namespaces the new external_ids so they never collide."""
        self.state = GridState.REBALANCING
        try:
            await self._cancel_own()
            if self.bias_fn is not None:  # dynamic mode: re-read the regime each re-center
                try:
                    new_bias = await self.bias_fn(self.bias)
                    if new_bias != self.bias:
                        print(f"  ~ bias {self.bias:+d} -> {new_bias:+d} (regime shift)")
                        self.bias = new_bias
                except Exception as e:  # noqa: BLE001 — a dead signal must not stop the re-center
                    print(f"  ! bias_fn failed ({e}) — keeping bias {self.bias:+d}")
            self.center = new_mid
            self.lower = new_mid * self._lower_ratio
            self.upper = new_mid * self._upper_ratio
            self.levels = [quantize(p, self.tick)
                           for p in levels_for(self.lower, self.upper, self.cfg.levels, self.cfg.spacing)]
            self._gen += 1
            orders = build_grid_orders(self.cfg, self.levels, new_mid, self._gen, bias=self.bias)
            net = self.net_inventory()
            if self.bias != 0 and net != 0 and (net > 0) == (self.bias > 0):
                # Trend mode holds with-trend inventory: re-arm its take-profit
                # reducers. The bias only blocks OPENING against the trend — it must
                # never leave a held position without resting exits (a biased ladder
                # at the cap would otherwise re-lay ZERO orders).
                reduce_side = Side.SELL if net > 0 else Side.BUY
                full = build_grid_orders(self.cfg, self.levels, new_mid, self._gen)
                reducers = [o for o in full if o.side is reduce_side]
                reducers.sort(key=lambda o: o.price, reverse=(reduce_side is Side.BUY))
                orders += reducers[:int(abs(net) / self.cfg.order_size)]
            if net > 0:    # long: don't average UP (no BUY above the entry)
                orders = [o for o in orders if not (o.side is Side.BUY and o.price > self.pos_avg)]
            elif net < 0:  # short: don't average DOWN (no SELL below the entry)
                orders = [o for o in orders if not (o.side is Side.SELL and o.price < self.pos_avg)]
            orders = self._respect_inventory_cap(orders, net)
            await self._place_many(orders)
            print(f"  ~ recenter -> mid {new_mid} band [{self.lower}, {self.upper}] gen {self._gen} "
                  f"({len(orders)} orders, net_inv {net}, bias {self.bias:+d})")
        finally:
            if self.state is GridState.REBALANCING:  # ALWAYS leave a runnable state (never orphan)
                self.state = GridState.RUNNING

    def _respect_inventory_cap(self, orders: list[Order], net: Decimal) -> list[Order]:
        """Cap-aware ladder thinning (issue #34): a re-center must never re-arm the
        side that grows |inventory| past the breaker cap — that manufactures the
        very breach the breaker exists to stop. Keeps only as many
        inventory-increasing orders as the remaining cap room allows, nearest to
        mid first (they fill first)."""
        cap = self.breaker.max_inventory if self.breaker is not None else Decimal(0)
        if cap <= 0 or self.cfg.order_size <= 0 or net == 0:
            return orders
        room = int((cap - abs(net)) / self.cfg.order_size)
        growing = Side.BUY if net > 0 else Side.SELL
        ladder = [o for o in orders if o.side is growing]
        if len(ladder) <= room:
            return orders
        ladder.sort(key=lambda o: o.price, reverse=(growing is Side.BUY))  # nearest mid first
        keep = {id(o) for o in ladder[:max(room, 0)]}
        dropped = len(ladder) - len(keep)
        print(f"  ~ thinned {dropped} {growing.value} order(s): inventory {net} near cap {cap}")
        return [o for o in orders if o.side is not growing or id(o) in keep]

    async def _cancel_own(self) -> None:
        """Cancel only THIS instance's resting orders, so two grids sharing one
        market+account never wipe each other on a re-center or stop. (Emergency
        `_exit` still nukes the whole market on purpose — flatten closes the
        shared net position anyway.) Falls back to market-wide cancel_all when
        the venue cannot enumerate open orders."""
        self._resting.clear()  # everything of ours is being cancelled below
        try:
            resting = await self.ex.open_orders(self.cfg.market)
        except Exception as e:  # noqa: BLE001
            print(f"  ! open_orders failed ({e}) — falling back to cancel_all")
            await self.ex.cancel_all(self.cfg.market)
            return
        mine = [o for o in resting if o.instance_id == self.cfg.instance_id]
        if not mine:
            return
        batch = getattr(self.ex, "cancel_orders", None)
        if batch is not None:  # venue batch endpoint: 1-2 round-trips for the lot
            await batch(self.cfg.market, mine)
            return
        for o in mine:
            try:
                await self.ex.cancel_order(self.cfg.market, o.order_id or o.external_id)
            except Exception as e:  # noqa: BLE001 — an order that just filled is fine
                print(f"  ! cancel {o.external_id} failed: {e}")

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
                await self._exit(reason, "circuit-breaker", mark)
                return
        if self.profit_guard is not None and mark is not None:
            reason = self.profit_guard.check(pnl)
            if reason:
                await self._exit(reason, "profit-lock", mark)

    async def _exit(self, reason: str, kind: str, mark: Decimal | None = None) -> None:
        """Emergency/profit exit: stop the grid, cancel everything, flatten."""
        if self.state is GridState.HALTED:
            return
        self.state = GridState.HALTED
        self.exit_reason = reason
        self.exit_kind = kind
        self._resting.clear()
        print(f"  !! {kind}: {reason} — cancel_all + flatten")
        await self._retry_exit_step("cancel_all", self.ex.cancel_all)
        if await self._retry_exit_step("flatten", self.ex.flatten):
            self._realize_exit(mark)

    def _realize_exit(self, mark: Decimal | None) -> None:
        """Book a flatten into realized PnL. The engine's number must match the
        venue's: live 2026-06-11 a breaker flatten cost -0.354 that outcome()
        never saw, so the episode attested 'winrate 100% pnl +0.23' while the
        account lost money. Priced at the mark that triggered the exit — the
        venue's taker fill differs by slippage only."""
        if self.pos_qty == 0:
            return
        if mark is None:
            print(f"  ! flatten not priced — realized PnL excludes the last "
                  f"{self.pos_qty} position")
            self.pos_qty, self.pos_avg = Decimal(0), Decimal(0)
            return
        side = Side.SELL if self.pos_qty > 0 else Side.BUY
        self._apply_fill(side, mark, abs(self.pos_qty))

    async def _retry_exit_step(self, what: str, op, attempts: int = 3) -> bool:
        """An exit step MUST land: once HALTED the monitor stops, so a tripped
        breaker with a live position is unsupervised risk. Retry with backoff;
        shout if the venue still refuses — that needs a human."""
        delay = self._exit_retry_delay
        for attempt in range(1, attempts + 1):
            try:
                await op(self.cfg.market)
                return True
            except Exception as e:  # noqa: BLE001
                print(f"    {what} failed (attempt {attempt}/{attempts}): {e}")
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay *= 2
        print(f"  !!! {what} did not land after {attempts} attempts — "
              f"POSITION MAY STILL BE OPEN on {self.cfg.market}; close it manually")
        return False

    async def handle_fill(self, fill: Fill) -> Order | None:
        if fill.instance_id != self.cfg.instance_id:
            return None
        if self.store is not None:
            await self.store.record_fill(fill)  # persist BEFORE placing the pair
        self._fills.append(fill)
        self.fill_count += 1
        self._resting.pop(fill.external_id, None)
        self._apply_fill(fill.side, fill.price, fill.qty)
        await self._check_guards(fill.price)  # risk + profit guards before placing more orders
        if self.state is not GridState.RUNNING:
            return None
        self._nonce += 1
        paired = compute_paired_order(self.cfg, self.levels, fill.level, fill.side, self._nonce)
        if paired is not None and not self._fits_cap(paired):
            print(f"  ~ skipped paired {paired.side.value} L{paired.level}: "
                  f"resting ladder would exceed inventory cap")
            return None
        if paired is not None:
            await self._place(paired)
        return paired

    def _fits_cap(self, order: Order) -> bool:
        """Placement-time cap budget (issue #36): would |position| stay within the
        breaker cap even if EVERY resting inventory-increasing order — plus this
        one — filled in a single sweep? Reducing orders always fit."""
        cap = self.breaker.max_inventory if self.breaker is not None else Decimal(0)
        if cap <= 0:
            return True
        pos = self.pos_qty
        growing = Side.BUY if pos >= 0 else Side.SELL
        if order.side is not growing:
            return True
        resting_growth = sum(o.qty for o in self._resting.values() if o.side is growing)
        return abs(pos) + resting_growth + order.qty <= cap

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
        await self._cancel_own()  # instance-scoped: never wipes a sibling grid
        await self._close_flat()
        self.state = GridState.HALTED

    async def _close_flat(self) -> None:
        """A graceful close must not leave an unsupervised position behind (live
        2026-06-10: 99 MNT long survived a close with ZERO take-profit orders).
        Flatten and book the exit at the current mid so the attested PnL
        includes it. A guard exit that landed has already flattened and realized
        (pos_qty 0); a guard flatten that FAILED leaves pos_qty set, and this is
        the retry that catches it."""
        if self.pos_qty == 0:
            return
        mark: Decimal | None = None
        try:
            bid, ask = await self.ex.best_bid_ask(self.cfg.market)
            mark = (bid + ask) / 2
        except Exception:  # noqa: BLE001 — flatten anyway; the exit just goes unpriced
            pass
        try:
            await self.ex.flatten(self.cfg.market)
        except Exception as e:  # noqa: BLE001
            print(f"  !!! close flatten failed — POSITION MAY STILL BE OPEN on "
                  f"{self.cfg.market}: {e}")
            return
        self._realize_exit(mark)

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
