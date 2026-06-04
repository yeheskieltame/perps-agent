"""Runnable dry-run of the Verifiable Learning Loop on the in-memory FakeExchange
+ in-memory chain — no keys, no network. Proves the architecture end to end.

    PYTHONPATH=src python3 scripts/demo_dry_run.py
"""
import asyncio
from decimal import Decimal

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.agent.recall import recall_best
from perpsagent.agent.sense import classify_regime
from perpsagent.agent.loop import LearningLoop
from perpsagent.app.manager import GridManager

MARKET = "BTCUSDT"


async def _settle():
    for _ in range(80):
        await asyncio.sleep(0)


async def episode(loop, ex, label):
    await ex.move_price(MARKET, Decimal("100"))  # reset to mid
    regime = await classify_regime(ex, MARKET)
    recalled = await recall_best(loop.chain, regime)
    iid, cfg, tx = await loop.plan_and_launch(MARKET)
    print(f"{label}")
    print(f"  recall: {len(recalled)} prior verified episode(s) for this regime")
    print(f"  commit: {tx[:18]}…  grid [{cfg.lower:.2f}, {cfg.upper:.2f}] x{cfg.levels} ({cfg.spacing.value})")
    for px in ("99.3", "100.7", "99.3", "100.7"):  # oscillate inside the band
        await ex.move_price(MARKET, Decimal(px))
        await _settle()
    out = await loop.close_and_learn(iid)
    print(f"  result: fills={out.fill_count} winrate={out.winrate:.0%} "
          f"realizedPnL={out.realized_pnl} riskAdj={out.risk_adjusted:.4f}  (attested on-chain)")


async def main():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    chain = MemoryChain()
    loop = LearningLoop(ex, chain, GridManager())

    await episode(loop, ex, "Episode 1  (cold start — empty memory)")
    await episode(loop, ex, "Episode 2  (warm — recalls verified memory)")

    total = sum(len(v) for v in chain._memory.values())
    print(f"\nStrategyMemory now holds {total} verified record(s) — the agent's growing brain.")


if __name__ == "__main__":
    asyncio.run(main())
