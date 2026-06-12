"""Recovery: finish episodes that were interrupted before their on-chain close.

Finds RUNNING instances in the SQLite store, rebuilds the outcome from persisted
fills (rehydrate), then: attest (skipped if already attested) → write StrategyMemory
→ mark CLOSED. Regime comes from the stored launch regime; if absent (older rows),
it is re-sampled from live signals now (flagged as approximation).

    cd backend && PYTHONPATH=src python3 scripts/close_episode.py

Pass instance ids to recover SPECIFIC episodes regardless of stored state —
an episode can be CLOSED in the store yet missing its attest (live 2026-06-11:
two attest txs died in a nonce race AFTER the store flipped to CLOSED):

    PYTHONPATH=src python3 scripts/close_episode.py MNTUSDT-1781112143940-1 ...

`--no-memory` recovers the attestation only — for an episode whose write_memory
DID land (re-writing would duplicate the StrategyMemory record and overweight
that config in recall).
"""
import asyncio
import json
import sys

sys.path.insert(0, "src")
from perpsagent.adapters.chain.client import MantleChainClient  # noqa: E402
from perpsagent.adapters.exchanges.fake import FakeExchange  # noqa: E402
from perpsagent.adapters.signals.elfa import ElfaSignals  # noqa: E402
from perpsagent.adapters.signals.nansen import NansenSignals  # noqa: E402
from perpsagent.adapters.signals.surf import SurfSignals  # noqa: E402
from perpsagent.adapters.store.sqlite_store import SqliteStore  # noqa: E402
from perpsagent.agent.learn import to_record  # noqa: E402
from perpsagent.agent.sense import classify_regime  # noqa: E402
from perpsagent.app.engine import GridEngine  # noqa: E402
from perpsagent.config import Settings  # noqa: E402
from perpsagent.domain.models import RegimeFingerprint  # noqa: E402


async def main() -> None:
    s = Settings()
    store = SqliteStore(s.store_db_path)
    chain = MantleChainClient(
        s.mantle_rpc, s.mantle_private_key, s.strategy_ledger_addr,
        s.strategy_memory_addr, s.vault_addr or None,
        detail_path=s.memory_detail_path or None,
    )
    args = sys.argv[1:]
    write_mem = "--no-memory" not in args
    wanted = [a for a in args if not a.startswith("--")]
    if wanted:  # targeted recovery: by id, regardless of stored state
        from perpsagent.adapters.store.serde import json_to_cfg
        open_instances = []
        for iid in wanted:
            row = store._conn.execute(  # noqa: SLF001 — ops script, direct read is fine
                "SELECT config FROM instances WHERE instance_id=?", (iid,)).fetchone()
            if row is None:
                print(f"!! {iid}: not in the store — skipped")
                continue
            open_instances.append(json_to_cfg(row[0]))
    else:
        open_instances = await store.load_open_instances()
    if not open_instances:
        print("no matching instances — nothing to close")
        return
    for cfg in open_instances:
        iid = cfg.instance_id
        fills = await store.load_fills(iid)
        eng = GridEngine(FakeExchange({}), cfg, store)
        eng.rehydrate(fills)
        out = eng.outcome()
        print(f"== {iid}: fills={out.fill_count} winrate={out.winrate:.0%} pnl={out.realized_pnl}")

        att = chain.ledger.functions.getLatestAttestation(MantleChainClient._instance_b32(iid)).call()
        if att[5] > 0:  # count
            print(f"   attest: already on-chain (count={att[5]}) — skip")
        else:
            print("   attest:", (await chain.attest(iid, out))[:18], "…")

        rj = await store.load_regime(iid)
        if rj:
            regime = RegimeFingerprint(**json.loads(rj))
            src = "stored launch regime"
        else:
            sigs = []
            if s.surf_api_key: sigs.append(SurfSignals({"api_key": s.surf_api_key, "base_url": s.surf_base_url}))
            if s.elfa_api_key: sigs.append(ElfaSignals({"api_key": s.elfa_api_key}))
            if s.nansen_api_key: sigs.append(NansenSignals({"api_key": s.nansen_api_key}))
            regime = await classify_regime(FakeExchange({}), cfg.market, sigs)
            src = "re-sampled now (approximation — launch regime was not persisted)"
        print(f"   regime ({src}): {regime}")
        if write_mem:
            print("   write_memory:", (await chain.write_memory(to_record(regime, cfg, out)))[:18], "…")
        else:
            print("   write_memory: skipped (--no-memory)")
        await store.set_state(iid, "CLOSED")
        print("   state: CLOSED ✓")
    if hasattr(chain, "drain"):  # let fire-then-confirm txs land before exit
        await chain.drain()


if __name__ == "__main__":
    asyncio.run(main())
