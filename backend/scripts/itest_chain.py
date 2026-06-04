"""Integration check: MantleChainClient against live (anvil) deployed contracts.
Env: PERPSAGENT_RPC, PERPSAGENT_PRIVATE_KEY, LEDGER, MEMORY, VAULT."""
import asyncio
import os
import time
from decimal import Decimal

from perpsagent.adapters.chain.client import MantleChainClient
from perpsagent.domain.models import (
    EpisodeOutcome, GridConfig, MemoryQuery, MemoryRecord, RegimeFingerprint, Spacing, Venue,
)


async def main():
    c = MantleChainClient(
        rpc_url=os.environ["PERPSAGENT_RPC"],
        private_key=os.environ["PERPSAGENT_PRIVATE_KEY"],
        ledger_addr=os.environ["LEDGER"],
        memory_addr=os.environ["MEMORY"],
        vault_addr=os.environ.get("VAULT"),
        detail_path="/tmp/perpsagent_mirror.json",
    )
    print("chainId", c.chain_id, "deployer", c.acct.address[:10] + "…")

    run_id = f"BTCUSDT-itest-{int(time.time())}"
    cfg = GridConfig(run_id, Venue.MANTLE_DEX, "BTCUSDT",
                     Decimal("99"), Decimal("101"), 10, Decimal("0.01"), Spacing.GEOMETRIC)
    regime = RegimeFingerprint(round(0.2 + (int(time.time()) % 97) / 100, 2), 0.0, 0.0001, 0.02, 0.0)
    outcome = EpisodeOutcome(cfg.instance_id, Decimal("1.50"), 0.66, 0.0, 12, "0x" + "ab" * 32, 0.83, False)

    print("commit:", (await c.commit_strategy(cfg.instance_id, cfg))[:18], "…")
    print("attest:", (await c.attest(cfg.instance_id, outcome))[:18], "…")
    print("write :", (await c.write_memory(MemoryRecord(regime=regime, config=cfg, outcome=outcome)))[:18], "…")

    recalled = []
    for _ in range(8):  # public RPCs are load-balanced; tolerate read-after-write lag
        recalled = await c.recall(MemoryQuery(regime=regime, k=8))
        if recalled:
            break
        await asyncio.sleep(2)
    assert len(recalled) == 1, f"expected 1 recalled, got {len(recalled)}"
    r = recalled[0]
    assert r.config.market == "BTCUSDT" and r.config.levels == 10, r.config
    assert abs(r.outcome.winrate - 0.66) < 1e-9, r.outcome.winrate
    assert abs(r.outcome.risk_adjusted - 0.83) < 1e-9, r.outcome.risk_adjusted
    assert r.outcome.realized_pnl == Decimal("1.5"), r.outcome.realized_pnl

    # read raw on-chain commitment to prove the write landed
    com = c.ledger.functions.getCommitment(MantleChainClient._instance_b32(cfg.instance_id)).call()
    assert com[0].lower() == c.acct.address.lower(), com
    print("recall OK -> market", r.config.market, "levels", r.config.levels,
          "winrate", r.outcome.winrate, "riskAdj", r.outcome.risk_adjusted, "pnl", r.outcome.realized_pnl)
    print("INTEGRATION OK")



if __name__ == "__main__":
    asyncio.run(main())
