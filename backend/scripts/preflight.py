"""Perps Agent preflight — verify every live connection before a demo/run.

    PYTHONPATH=src python3 scripts/preflight.py

Reads backend/.env (Settings). Prints a PASS/FAIL/SKIP matrix; exit 1 on FAIL.
Run this on a machine with open egress (Bybit is blocked from some sandboxes).
"""
import asyncio
import sys

sys.path.insert(0, "src")
from perpsagent.config import Settings  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def add(name: str, status: str, info: str = "") -> None:
    results.append((name, status, info))
    print(f"[{status:^4}] {name}{('  — ' + info) if info else ''}")


async def check_bybit(s: Settings) -> None:
    import aiohttp

    base = "https://api-testnet.bybit.com" if s.bybit_testnet else "https://api.bybit.com"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{base}/v5/market/time", timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
        add("bybit public reachability", PASS if d.get("retCode") == 0 else FAIL, base)
    except Exception as e:
        add("bybit public reachability", FAIL, f"{type(e).__name__}: {e}")
        return
    if not (s.bybit_api_key and s.bybit_api_secret):
        add("bybit private (signed balance)", SKIP, "set PERPSAGENT_BYBIT_API_KEY/SECRET in backend/.env")
        return
    try:
        from perpsagent.adapters.exchanges.bybit.adapter import BybitExchange

        ex = BybitExchange({"api_key": s.bybit_api_key, "api_secret": s.bybit_api_secret,
                            "testnet": s.bybit_testnet})
        bal = await ex.balance()
        await ex.close()
        add("bybit private (signed balance)", PASS, f"equity={bal.equity} {bal.currency}")
    except Exception as e:
        add("bybit private (signed balance)", FAIL, f"{type(e).__name__}: {e}")


async def check_mantle(s: Settings) -> None:
    if not s.mantle_rpc:
        add("mantle rpc", SKIP, "PERPSAGENT_MANTLE_RPC empty")
        return
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(s.mantle_rpc))
        cid = w3.eth.chain_id
        add("mantle rpc", PASS, f"chainId={cid} block={w3.eth.block_number}")
        for label, addr in [("StrategyLedger", s.strategy_ledger_addr),
                            ("StrategyMemory", s.strategy_memory_addr),
                            ("Vault", s.vault_addr)]:
            if not addr:
                add(f"contract {label}", SKIP, "address not set")
                continue
            code = w3.eth.get_code(Web3.to_checksum_address(addr))
            add(f"contract {label}", PASS if len(code) > 2 else FAIL, addr)
        if s.strategy_memory_addr and s.mantle_private_key:
            from perpsagent.adapters.chain.client import MantleChainClient

            c = MantleChainClient(s.mantle_rpc, s.mantle_private_key,
                                  s.strategy_ledger_addr, s.strategy_memory_addr,
                                  s.vault_addr or None)
            n = c.memory.functions.countByRegime(b"\x00" * 32).call()
            add("chain client read (countByRegime)", PASS, f"zero-key count={n}")
    except Exception as e:
        add("mantle rpc", FAIL, f"{type(e).__name__}: {e}")


async def check_signals(s: Settings) -> None:
    from perpsagent.adapters.signals.elfa import ElfaSignals
    from perpsagent.adapters.signals.nansen import NansenSignals
    from perpsagent.adapters.signals.surf import SurfSignals

    for name, key, client in [("elfa", s.elfa_api_key, ElfaSignals({"api_key": s.elfa_api_key})),
                              ("nansen", s.nansen_api_key, NansenSignals({"api_key": s.nansen_api_key})),
                              ("surf", s.surf_api_key, SurfSignals({"api_key": s.surf_api_key, "base_url": s.surf_base_url}))]:
        if not key:
            add(f"signal {name}", SKIP, "no API key in backend/.env")
            continue
        snap = await client.snapshot("BTCUSDT")
        add(f"signal {name}", PASS if snap else FAIL, str(snap) if snap else "empty/failed (fail-soft)")


async def main() -> None:
    s = Settings()
    s.assert_consistent()
    print(f"== Perps Agent preflight (env={s.env}) ==")
    await check_bybit(s)
    await check_mantle(s)
    await check_signals(s)
    fails = [r for r in results if r[1] == FAIL]
    print(f"\n{len([r for r in results if r[1]==PASS])} pass, {len(fails)} fail, "
          f"{len([r for r in results if r[1]==SKIP])} skipped")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
