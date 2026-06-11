"""The Verifiable Learning Loop (docs/CONCEPT.md §3).

plan_and_launch:  SENSE -> RECALL -> DECIDE -> COMMIT(on-chain) -> EXECUTE
close_and_learn:  ATTEST(on-chain) -> write StrategyMemory (LEARN)

Mantle is the hinge: recall reads verified experience (analysis), the same
records train the policy (strategy), attestation makes it auditable (verification).
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

from ..domain.models import EpisodeOutcome, GridConfig, RegimeFingerprint, Venue
from .decide import ContextualPolicy
from .learn import to_record
from .reason import explain_decision
from .recall import recall_best
from .sense import classify_regime


class LearningLoop:
    def __init__(self, exchange, chain, manager, policy: ContextualPolicy | None = None,
                 signals: list | None = None, venue: Venue = Venue.FAKE, store=None,
                 breaker=None, recenter_interval: float = 0.0, profit_guard=None) -> None:
        self.exchange = exchange
        self.chain = chain
        self.manager = manager
        self.policy = policy or ContextualPolicy()
        self.signals = signals or []
        self.venue = venue
        self.store = store
        self.breaker = breaker
        self.profit_guard = profit_guard
        self.recenter_interval = recenter_interval  # >0 enables the live re-center supervisor
        self._regimes: dict[str, RegimeFingerprint] = {}
        self._seq = 0
        self._rationales: dict[str, str] = {}
        # Whether the LAST close_and_learn landed its attest tx. Callers report
        # from this — never print "attested" when the tx failed (live 2026-06-11:
        # two episodes lost to a nonce race were announced as attested).
        self.last_attest_ok: bool = False

    async def plan_and_launch(self, market: str, leverage: Decimal = Decimal(1)) -> tuple[str, GridConfig, str]:
        regime = await classify_regime(self.exchange, market, self.signals)
        recalled = await recall_best(self.chain, regime)
        bid, ask = await self.exchange.best_bid_ask(market)
        mid = (bid + ask) / 2
        self._seq += 1
        instance_id = f"{market}-{int(time.time() * 1000)}-{self._seq}"
        cfg = self.policy.propose(instance_id, market, mid, recalled, venue=self.venue,
                                  leverage=leverage, regime=regime)
        if self.breaker is not None and self.breaker.max_inventory <= 0:
            self.breaker.max_inventory = cfg.order_size * cfg.levels * 3  # default cap: 3x nominal one-sided inv
        overrides = "/".join(self.policy.pinned()) if (recalled and self.policy.pinned()) else ""
        self._rationales[instance_id] = explain_decision(regime, recalled, cfg, overrides)
        tx = await self.chain.commit_strategy(instance_id, cfg)  # pre-commit BEFORE trading

        async def bias_fn(current: int) -> int:
            """Re-read the regime on each re-center so the grid morphs ranging<->trend
            mid-episode (hysteresis lives in the policy)."""
            live = await classify_regime(self.exchange, market, self.signals)
            return self.policy.bias_for(live.trend_strength, prev_bias=current)

        await self.manager.create(self.exchange, cfg, self.store,  # execute the grid
                                  breaker=self.breaker, monitor_interval=self.recenter_interval,
                                  profit_guard=self.profit_guard, bias_fn=bias_fn)
        if self.store is not None and hasattr(self.store, "save_instance"):
            await self.store.save_instance(cfg, regime_json=json.dumps(regime.__dict__))
        self._regimes[instance_id] = regime
        return instance_id, cfg, tx

    async def recover(self):
        """Rebuild open instances from the store after a restart (replays PnL)."""
        return await self.manager.recover(self.store, self.exchange) if self.store else []

    def rationale(self, instance_id: str) -> str:
        return self._rationales.get(instance_id, "")

    async def close_and_learn(self, instance_id: str) -> EpisodeOutcome:
        engine = self.manager.get(instance_id)
        if engine is None:
            raise KeyError(instance_id)
        await self.manager.stop(instance_id)  # cancel orders + mark CLOSED in store
        outcome = engine.outcome()
        try:
            await self.chain.attest(instance_id, outcome)  # attest verified outcome
            self.last_attest_ok = True
        except Exception as e:  # noqa: BLE001 — keep closing even if one step fails
            self.last_attest_ok = False
            print(f"  ! attest failed: {e}")
        regime = self._regimes.get(instance_id)
        if regime is not None:
            try:
                await self.chain.write_memory(to_record(regime, engine.cfg, outcome))  # learn
                self.policy.update([])
            except Exception as e:  # noqa: BLE001
                print(f"  ! write_memory failed: {e}")
        return outcome
