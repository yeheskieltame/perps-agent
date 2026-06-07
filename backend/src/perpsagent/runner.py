"""Process entrypoint. Wires Settings -> adapters -> agent loop.

    python -m perpsagent.runner --mode dry  --market BTCUSDT
    python -m perpsagent.runner --mode live --venue bybit       --market BTCUSDT
    python -m perpsagent.runner --mode live --venue mantle_dex  --market WMNTUSDT   # on-chain grid (iZiSwap)

dry  = FakeExchange + in-memory chain (no keys/network) — self-demonstrates.
live = Bybit v5 OR iZiSwap (Mantle on-chain) for execution + MantleChainClient for
       the verifiable memory/ledger. Same engine + agent, swap the venue (modular).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal

from .agent.decide import ContextualPolicy
from .agent.loop import LearningLoop
from .app.manager import GridManager
from .app.safety import CircuitBreaker, ProfitGuard
from .config import Settings
from .domain.models import Venue


def _build(s: Settings, mode: str, venue_choice: str):
    if mode == "live":
        from .adapters.chain.client import MantleChainClient
        from .adapters.signals.elfa import ElfaSignals
        from .adapters.signals.nansen import NansenSignals
        from .adapters.signals.surf import SurfSignals

        chain = MantleChainClient(
            rpc_url=s.mantle_rpc, private_key=s.mantle_private_key,
            ledger_addr=s.strategy_ledger_addr, memory_addr=s.strategy_memory_addr,
            vault_addr=s.vault_addr or None, detail_path=s.memory_detail_path or None,
        )
        signals = []
        if s.elfa_api_key:
            signals.append(ElfaSignals({"api_key": s.elfa_api_key}))
        if s.nansen_api_key:
            signals.append(NansenSignals({"api_key": s.nansen_api_key}))
        if s.surf_api_key:
            signals.append(SurfSignals({"api_key": s.surf_api_key, "base_url": s.surf_base_url}))

        if s.postgres_dsn:  # durable multi-tenant backend (same StorePort)
            from .adapters.store.postgres_store import PostgresStore

            store = PostgresStore(s.postgres_dsn)
        else:
            from .adapters.store.sqlite_store import SqliteStore

            store = SqliteStore(s.store_db_path)
        if venue_choice == "mantle_dex":
            from .adapters.exchanges.mantle_dex.adapter import MantleDexExchange

            ex = MantleDexExchange({
                "rpc_url": s.mantle_rpc, "private_key": s.mantle_private_key,
                "markets": json.loads(s.iziswap_markets or "{}"),
            })
            return ex, chain, Venue.MANTLE_DEX, signals, store

        from .adapters.exchanges.bybit.adapter import BybitExchange

        ex = BybitExchange({
            "api_key": s.bybit_api_key, "api_secret": s.bybit_api_secret, "testnet": s.bybit_testnet,
            "rate_limit": s.bybit_rate_limit, "max_retries": s.bybit_max_retries,
        })
        return ex, chain, Venue.BYBIT, signals, store

    from .adapters.chain.memory_chain import MemoryChain
    from .adapters.exchanges.fake import FakeExchange

    return FakeExchange({"mid": "100", "tick": "0.1"}), MemoryChain(), Venue.FAKE, [], None


async def run(mode: str, market: str, venue_choice: str, leverage: Decimal = Decimal(1),
              recenter_interval: float = 0.0, max_inventory: str = "0", max_drawdown: str = "0",
              band: str = "0.01", levels: int = 10, order_size: str = "0.01",
              take_profit: str = "0", trail: str = "0", trail_arm: str = "0",
              pin_band: bool = False, pin_levels: bool = False, pin_order_size: bool = False) -> None:
    s = Settings()
    s.assert_consistent()
    ex, chain, venue, signals, store = _build(s, mode, venue_choice)

    # Guards + re-center are live-only (the dry demo drives price by hand).
    breaker = None
    profit_guard = None
    monitor_interval = 0.0
    if mode == "live":
        breaker = CircuitBreaker(max_inventory=Decimal(max_inventory), max_drawdown=Decimal(max_drawdown))
        profit_guard = ProfitGuard(take_profit=Decimal(take_profit), trail_frac=Decimal(trail),
                                   trail_arm=Decimal(trail_arm))
        monitor_interval = recenter_interval

    # Tunable grid shape (used when on-chain recall has no verified episode yet).
    policy = ContextualPolicy(default_band=Decimal(band), default_levels=levels, order_size=Decimal(order_size),
                              pin_band=pin_band, pin_levels=pin_levels, pin_order_size=pin_order_size)
    loop = LearningLoop(ex, chain, GridManager(), policy=policy, signals=signals, venue=venue, store=store,
                        breaker=breaker, recenter_interval=monitor_interval, profit_guard=profit_guard)
    recovered = await loop.recover()
    if recovered:
        print(f"[{mode}] recovered {len(recovered)} open instance(s) from store")

    iid, cfg, tx = await loop.plan_and_launch(market, leverage=leverage)
    rc = f"every {monitor_interval:g}s" if monitor_interval > 0 else "off"
    cap = breaker.max_inventory if breaker else "-"
    pg = (f"tp={profit_guard.take_profit} trail={profit_guard.trail_frac}"
          if profit_guard and (profit_guard.take_profit or profit_guard.trail_frac) else "off")
    print(f"[{mode}/{venue.value}] launched {iid}  commit={tx[:18]}…  grid [{cfg.lower}, {cfg.upper}] "
          f"x{cfg.levels}  lev={cfg.leverage}x  recenter={rc}  invCap={cap}  profit-lock={pg}")

    print(f"  rationale: {loop.rationale(iid)}")

    if mode == "dry" and hasattr(ex, "move_price"):
        for px in ("99.3", "100.7", "99.3", "100.7"):
            await ex.move_price(market, Decimal(px))
            for _ in range(60):
                await asyncio.sleep(0)
        out = await loop.close_and_learn(iid)
        print(f"[dry] episode done: fills={out.fill_count} winrate={out.winrate:.0%} "
              f"pnl={out.realized_pnl} riskAdj={out.risk_adjusted:.4f}")
    else:
        print(f"[live] running on {venue.value} — Ctrl-C to stop")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("[live] closing gracefully — cancel orders → attest → write memory. JANGAN Ctrl-C lagi…")
            out = await asyncio.shield(loop.close_and_learn(iid))
            if hasattr(loop.chain, "drain"):  # let fire-then-confirm attest/memory land
                await asyncio.shield(loop.chain.drain())
            print(f"[live] episode attested: fills={out.fill_count} winrate={out.winrate:.0%} pnl={out.realized_pnl}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="perpsagent.runner")
    ap.add_argument("--mode", choices=["dry", "live"], default="dry")
    ap.add_argument("--venue", choices=["bybit", "mantle_dex"], default="bybit")
    ap.add_argument("--market", default="BTCUSDT")
    ap.add_argument("--leverage", default=None, help="user-chosen leverage, e.g. 10 (default: .env PERPSAGENT_LEVERAGE or 1)")
    ap.add_argument("--recenter-interval", type=float, default=None,
                    help="live re-center cadence in seconds; 0 disables (default: .env or 15)")
    ap.add_argument("--max-inventory", default=None, help="circuit-breaker net-position cap; 0 = auto from grid size")
    ap.add_argument("--max-drawdown", default=None, help="circuit-breaker loss cap in quote units; 0 = disabled")
    ap.add_argument("--band", default=None, help="grid half-band fraction of mid, e.g. 0.008 = +/-0.8 pct (default 0.01)")
    ap.add_argument("--levels", type=int, default=None, help="number of grid levels (default 10)")
    ap.add_argument("--order-size", default=None, help="base qty per level, e.g. 0.005 (default 0.01)")
    ap.add_argument("--take-profit", default=None, help="bank when total PnL >= this (quote units); 0 = off")
    ap.add_argument("--trail", default=None, help="trailing-stop: bank after giving back this fraction of peak PnL, e.g. 0.3; 0 = off")
    ap.add_argument("--trail-arm", default=None, help="peak PnL that must be reached before the trailing stop arms")
    args = ap.parse_args()

    s = Settings()  # defaults for any flag left unset
    leverage = Decimal(args.leverage if args.leverage is not None else (s.leverage or "1"))
    recenter = args.recenter_interval if args.recenter_interval is not None else s.recenter_interval_s
    max_inv = args.max_inventory if args.max_inventory is not None else s.max_inventory
    max_dd = args.max_drawdown if args.max_drawdown is not None else s.max_drawdown
    band = args.band if args.band is not None else "0.01"
    levels = args.levels if args.levels is not None else 10
    order_size = args.order_size if args.order_size is not None else "0.01"
    take_profit = args.take_profit if args.take_profit is not None else "0"
    trail = args.trail if args.trail is not None else "0"
    trail_arm = args.trail_arm if args.trail_arm is not None else "0"
    asyncio.run(run(args.mode, args.market, args.venue, leverage, recenter, max_inv, max_dd,
                    band, levels, order_size, take_profit, trail, trail_arm,
                    args.band is not None, args.levels is not None, args.order_size is not None))


if __name__ == "__main__":
    main()
