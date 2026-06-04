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

from .agent.loop import LearningLoop
from .app.manager import GridManager
from .config import Settings
from .domain.models import Venue


def _build(s: Settings, mode: str, venue_choice: str):
    if mode == "live":
        from .adapters.chain.client import MantleChainClient
        from .adapters.signals.elfa import ElfaSignals
        from .adapters.signals.nansen import NansenSignals
        from .adapters.signals.surf import SurfSignals
        from .adapters.store.sqlite_store import SqliteStore

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

        store = SqliteStore(s.store_db_path)
        if venue_choice == "mantle_dex":
            from .adapters.exchanges.mantle_dex.adapter import MantleDexExchange

            ex = MantleDexExchange({
                "rpc_url": s.mantle_rpc, "private_key": s.mantle_private_key,
                "markets": json.loads(s.iziswap_markets or "{}"),
            })
            return ex, chain, Venue.MANTLE_DEX, signals, store

        from .adapters.exchanges.bybit.adapter import BybitExchange

        ex = BybitExchange({"api_key": s.bybit_api_key, "api_secret": s.bybit_api_secret, "testnet": s.bybit_testnet})
        return ex, chain, Venue.BYBIT, signals, store

    from .adapters.chain.memory_chain import MemoryChain
    from .adapters.exchanges.fake import FakeExchange

    return FakeExchange({"mid": "100", "tick": "0.1"}), MemoryChain(), Venue.FAKE, [], None


async def run(mode: str, market: str, venue_choice: str) -> None:
    s = Settings()
    s.assert_consistent()
    ex, chain, venue, signals, store = _build(s, mode, venue_choice)
    loop = LearningLoop(ex, chain, GridManager(), signals=signals, venue=venue, store=store)
    recovered = await loop.recover()
    if recovered:
        print(f"[{mode}] recovered {len(recovered)} open instance(s) from store")

    iid, cfg, tx = await loop.plan_and_launch(market)
    print(f"[{mode}/{venue.value}] launched {iid}  commit={tx[:18]}…  grid [{cfg.lower}, {cfg.upper}] x{cfg.levels}")

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
            print(f"[live] episode attested: fills={out.fill_count} winrate={out.winrate:.0%} pnl={out.realized_pnl}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="perpsagent.runner")
    ap.add_argument("--mode", choices=["dry", "live"], default="dry")
    ap.add_argument("--venue", choices=["bybit", "mantle_dex"], default="bybit")
    ap.add_argument("--market", default="BTCUSDT")
    args = ap.parse_args()
    asyncio.run(run(args.mode, args.market, args.venue))


if __name__ == "__main__":
    main()
