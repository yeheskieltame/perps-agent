"""The Verifiable Learning Loop (docs.perpsagent.xyz).

plan_and_launch:  SENSE -> RECALL -> DECIDE -> COMMIT(on-chain) -> EXECUTE
close_and_learn:  ATTEST(on-chain) -> write StrategyMemory (LEARN)

Mantle is the hinge: recall reads verified experience (analysis), the same
records train the policy (strategy), attestation makes it auditable (verification).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.models import EpisodeOutcome, GridConfig, RegimeFingerprint, Venue
from .decide import ContextualPolicy
from .gates import LaunchGated, funding_gate, news_blackout
from .learn import to_record
from .reason import explain_decision
from .recall import recall_best
from .sense import classify_regime


class LearningLoop:
    def __init__(self, exchange, chain, manager, policy: ContextualPolicy | None = None,
                 signals: list | None = None, venue: Venue = Venue.FAKE, store=None,
                 breaker=None, recenter_interval: float = 0.0, profit_guard=None,
                 news_events: list | None = None, account_guard=None,
                 timeframe: str = "1") -> None:
        self.exchange = exchange
        self.chain = chain
        self.manager = manager
        self.policy = policy or ContextualPolicy()
        self.signals = signals or []
        self.venue = venue
        self.store = store
        self.breaker = breaker
        self.profit_guard = profit_guard
        self.news_events = news_events or []  # scheduled macro events (launch blackout)
        self.account_guard = account_guard
        # The user's operating timeframe (Bybit interval code): the grid senses
        # structure on the bars where its pattern lives — a 1m sensor on a 1h
        # structure reads micro-noise as regime shifts and flaps the bias
        # (live 2026-06-12). Full user choice; flows into SENSE + thesis-break.
        self.timeframe = timeframe
        self.recenter_interval = recenter_interval  # >0 enables the live re-center supervisor
        self._regimes: dict[str, RegimeFingerprint] = {}
        self._seq = 0
        self._rationales: dict[str, str] = {}
        # Whether the LAST close_and_learn landed its attest tx. Callers report
        # from this — never print "attested" when the tx failed (live 2026-06-11:
        # two episodes lost to a nonce race were announced as attested).
        self.last_attest_ok: bool = False

    async def plan_and_launch(self, market: str, leverage: Decimal = Decimal(1)) -> tuple[str, GridConfig, str]:
        """Raises LaunchGated (BEFORE the on-chain commit) when a deploy gate
        vetoes the episode — not deploying is a valid position."""
        block = news_blackout(datetime.now(timezone.utc), self.news_events)
        if block:
            raise LaunchGated(block)
        regime = await classify_regime(self.exchange, market, self.signals, timeframe=self.timeframe)
        recalled = await recall_best(self.chain, regime)
        bid, ask = await self.exchange.best_bid_ask(market)
        mid = (bid + ask) / 2
        self._seq += 1
        instance_id = f"{market}-{int(time.time() * 1000)}-{self._seq}"
        cfg = self.policy.propose(instance_id, market, mid, recalled, venue=self.venue,
                                  leverage=leverage, regime=regime)
        cfg.bias, gate_note = funding_gate(cfg.bias, regime.funding_rate)  # may raise LaunchGated
        if self.breaker is not None and self.breaker.max_inventory <= 0:
            self.breaker.max_inventory = cfg.order_size * cfg.levels * 3  # default cap: 3x nominal one-sided inv
        overrides = "/".join(self.policy.pinned()) if (recalled and self.policy.pinned()) else ""
        rationale = explain_decision(regime, recalled, cfg, overrides)
        if gate_note:
            rationale += f" | funding gate: {gate_note}"
        self._rationales[instance_id] = rationale
        tx = await self.chain.commit_strategy(instance_id, cfg)  # pre-commit BEFORE trading

        async def bias_fn(current: int) -> int:
            """Re-read the regime on each re-center so the grid morphs ranging<->trend
            mid-episode (hysteresis lives in the policy). The funding gate applies
            to morphs too: extreme funding mid-episode demotes to symmetric (the
            held side stays protected by thesis-break/breaker, never re-armed
            into a crowded squeeze)."""
            live = await classify_regime(self.exchange, market, self.signals,
                                         timeframe=self.timeframe)
            b = self.policy.bias_for(live.trend_strength, prev_bias=current,
                                     range_position=live.range_position)
            try:
                b, note = funding_gate(b, live.funding_rate)
            except LaunchGated as e:
                print(f"  ~ funding gate (mid-episode): {e.reason} — bias -> 0")
                return 0
            if note:
                print(f"  ~ funding gate: {note}")
            return b

        await self.manager.create(self.exchange, cfg, self.store,  # execute the grid
                                  breaker=self.breaker, monitor_interval=self.recenter_interval,
                                  profit_guard=self.profit_guard, bias_fn=bias_fn,
                                  account_guard=self.account_guard, timeframe=self.timeframe)
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
