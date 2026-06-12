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
import signal
from decimal import Decimal

from .agent.decide import ContextualPolicy
from .agent.gates import LaunchGated, parse_news_events
from .agent.loop import LearningLoop
from .agent.sense import tf_minutes
from .app.manager import GridManager
from .app.safety import AccountGuard, CircuitBreaker, ProfitGuard
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
              pin_band: bool = False, pin_levels: bool = False, pin_order_size: bool = False,
              bias_mode: str = "auto", account_drawdown: str = "0",
              timeframe: str = "1") -> None:
    s = Settings()
    s.assert_consistent()
    ex, chain, venue, signals, store = _build(s, mode, venue_choice)

    # Guards + re-center are live-only (the dry demo drives price by hand).
    breaker = None
    profit_guard = None
    account_guard = None
    monitor_interval = 0.0
    if mode == "live":
        breaker = CircuitBreaker(max_inventory=Decimal(max_inventory), max_drawdown=Decimal(max_drawdown))
        profit_guard = ProfitGuard(take_profit=Decimal(take_profit), trail_frac=Decimal(trail),
                                   trail_arm=Decimal(trail_arm))
        if Decimal(account_drawdown) > 0:
            account_guard = AccountGuard(max_drop=Decimal(account_drawdown))
        # The supervisor cadence follows the user's timeframe: a 1h grid checked
        # every 15s reacts to noise the user chose to ignore. bar/4, floor 15s.
        monitor_interval = recenter_interval if recenter_interval > 0 else max(
            15.0, tf_minutes(timeframe) * 60 / 4)

    # Tunable grid shape (used when on-chain recall has no verified episode yet).
    policy = ContextualPolicy(default_band=Decimal(band), default_levels=levels, order_size=Decimal(order_size),
                              pin_band=pin_band, pin_levels=pin_levels, pin_order_size=pin_order_size,
                              bias_mode=bias_mode)
    loop = LearningLoop(ex, chain, GridManager(), policy=policy, signals=signals, venue=venue, store=store,
                        breaker=breaker, recenter_interval=monitor_interval, profit_guard=profit_guard,
                        news_events=parse_news_events(s.news_events), account_guard=account_guard,
                        timeframe=timeframe)
    recovered = await loop.recover()
    if recovered:
        print(f"[{mode}] recovered {len(recovered)} open instance(s) from store")

    try:
        iid, cfg, tx = await loop.plan_and_launch(market, leverage=leverage)
    except LaunchGated as e:
        print(f"[{mode}] launch gated — NOT deploying: {e.reason}")
        return
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
        print(f"[live] running on {venue.value} — Ctrl-C or SIGTERM to stop")
        # Explicit handlers, not bare KeyboardInterrupt: a process launched in the
        # background (nohup/&, systemd, docker) inherits SIGINT=ignore and gets
        # SIGTERM on shutdown — both must still run the graceful close path.
        stop = asyncio.Event()
        runtime = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                runtime.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):  # non-unix / non-main thread
                pass
        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass  # fallback when no signal handler could be installed
        print("[live] closing gracefully — cancel orders → attest → write memory. JANGAN Ctrl-C lagi…")
        out = await asyncio.shield(loop.close_and_learn(iid))
        if hasattr(loop.chain, "drain"):  # let fire-then-confirm attest/memory land
            await asyncio.shield(loop.chain.drain())
        status = ("attested" if loop.last_attest_ok else
                  "closed — attest FAILED, recover with scripts/close_episode.py")
        print(f"[live] episode {status}: fills={out.fill_count} winrate={out.winrate:.0%} pnl={out.realized_pnl}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="perpsagent.runner")
    ap.add_argument("--mode", choices=["dry", "live"], default="dry")
    ap.add_argument("--venue", choices=["bybit", "mantle_dex"], default="bybit")
    ap.add_argument("--market", default="BTCUSDT")
    ap.add_argument("--timeframe", default=None,
                    help="operating timeframe — the bars where YOUR grid pattern lives (1m/5m/15m/1h/4h "
                         "or Bybit codes 1/5/15/60/240). Sensor, thesis-break, and recenter cadence all "
                         "follow it. Default: .env PERPSAGENT_TIMEFRAME or 1m")
    ap.add_argument("--leverage", default=None, help="user-chosen leverage, e.g. 10 (default: .env PERPSAGENT_LEVERAGE or 1)")
    ap.add_argument("--recenter-interval", type=float, default=None,
                    help="live re-center cadence in seconds; 0 disables (default: .env or 15)")
    ap.add_argument("--max-inventory", default=None, help="circuit-breaker net-position cap; 0 = auto from grid size")
    ap.add_argument("--max-drawdown", default=None, help="circuit-breaker loss cap in quote units; 0 = disabled")
    ap.add_argument("--account-drawdown", default=None,
                    help="account-level kill-switch: exit when WALLET equity drops this many quote "
                         "units below its episode-start level (all markets count); 0 = disabled")
    ap.add_argument("--band", default=None, help="grid half-band fraction of mid, e.g. 0.008 = +/-0.8 pct (default 0.01)")
    ap.add_argument("--levels", type=int, default=None, help="number of grid levels (default 10)")
    ap.add_argument("--order-size", default=None, help="base qty per level, e.g. 0.005 (default 0.01)")
    ap.add_argument("--take-profit", default=None, help="bank when total PnL >= this (quote units); 0 = off")
    ap.add_argument("--trail", default=None, help="trailing-stop: bank after giving back this fraction of peak PnL, e.g. 0.3; 0 = off")
    ap.add_argument("--trail-arm", default=None, help="peak PnL that must be reached before the trailing stop arms")
    ap.add_argument("--bias", choices=["auto", "long", "short", "neutral"], default="auto",
                    help="grid mode: auto = follow the regime (trend grid in trends, symmetric when ranging); "
                         "long/short/neutral pin it")
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
    acct_dd = args.account_drawdown if args.account_drawdown is not None else s.account_drawdown
    # Human timeframes (1m/5m/1h/4h/1d) map onto Bybit interval codes.
    _tf_alias = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                 "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D"}
    tf_raw = args.timeframe if args.timeframe is not None else (s.timeframe or "1")
    timeframe = _tf_alias.get(str(tf_raw).lower(), str(tf_raw))
    asyncio.run(run(args.mode, args.market, args.venue, leverage, recenter, max_inv, max_dd,
                    band, levels, order_size, take_profit, trail, trail_arm,
                    args.band is not None, args.levels is not None, args.order_size is not None,
                    args.bias, acct_dd, timeframe))


if __name__ == "__main__":
    main()
