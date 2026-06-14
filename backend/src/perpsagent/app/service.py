"""GridService — the ONLY seam the UI (Telegram / web) may depend on.

The bot must NEVER import the engine, adapters, or any exchange/chain SDK. Keep
this facade small, typed, and versioned (mirrors deltaperps).
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from ..agent.learn import to_record
from ..agent.sense import classify_regime
from ..domain.models import BalanceView, GridConfig, RegimeFingerprint
from .session import UserSession


class GridStatus(Protocol):
    instance_id: str
    state: str


@dataclass
class GridStatusView:
    instance_id: str
    state: str
    realized_pnl: str
    fill_count: int
    name: str = ""


@dataclass
class MarketView:
    market: str
    bid: str
    ask: str
    mid: str


class GridService(Protocol):
    async def create_grid(self, user_id: int, cfg: GridConfig) -> str: ...
    async def stop_grid(self, user_id: int, instance_id: str) -> None: ...
    async def pause_grid(self, user_id: int, instance_id: str) -> None: ...
    async def status(self, user_id: int) -> Sequence[GridStatus]: ...
    async def balance(self, user_id: int) -> BalanceView: ...
    async def market_info(self, user_id: int, market: str) -> MarketView: ...
    async def get_settings(self, user_id: int) -> dict: ...
    async def put_settings(self, user_id: int, settings: dict) -> None: ...
    async def reset_settings(self, user_id: int) -> None: ...


class AppService:
    """Concrete GridService — multi-tenant: one UserSession per user, each with its
    OWN venue client (own keys, own private fill stream). Capital and fills are
    isolated per user by construction (no shared account, no cross-user fan-out).

    Pass `client_factory(user_id) -> ExchangePort` for per-user clients (the
    product path), or a single `exchange` shared by all users (single-account /
    demo mode). Ownership is the session boundary: a user can only act on grids in
    their own session. Durable ownership across restarts is the next step
    (Postgres) — see plan/SCALING.md #8.
    """

    def __init__(self, exchange=None, store=None,
                 client_factory: Callable[[int], object] | None = None,
                 router=None, node=None, chain=None, signals=None,
                 builder_fee: int = 0, fee_asset: str = "", fee_account: str = "",
                 treasury: str = "", fee_payer=None) -> None:
        if exchange is None and client_factory is None:
            raise ValueError("AppService needs an `exchange` or a `client_factory`")
        self._exchange = exchange
        self._store = store
        self._client_factory = client_factory
        # Sharding (plan/SCALING.md #10): when this worker is one shard of many,
        # `router` + `node` let it serve/recover ONLY the users it owns. Both None
        # (single-node) → no shard filtering.
        self._router = router
        self._node = str(node) if node is not None else None
        self._sessions: dict[int, UserSession] = {}
        self._prefs: dict[int, dict] = {}  # settings fallback when the store has none
        # Verifiable Learning Loop (docs/CONCEPT.md §3), behind the GridService seam
        # so the UI never sees the chain. `chain` is a ChainPort (MantleChainClient
        # in prod, MemoryChain in tests); None disables proofs (dev/demo). ONE shared
        # signer for all users — proofs are signed by the operator, not the user
        # (users stay non-custodial on their own Bybit). `instance_id` is already
        # namespaced per user/node, so a single nonce lane stays race-free.
        self._chain = chain
        self._signals = signals or []
        self._regimes: dict[str, RegimeFingerprint] = {}  # instance_id -> regime at commit
        self._proofs: dict[str, dict] = {}  # instance_id -> {commit, attest, memory, fee} tx hashes
        # Builder fee (monetization): flat on-chain settlement per closed episode,
        # from the operator's bond to the treasury. 0 = off.
        self._builder_fee = int(builder_fee)
        self._fee_asset = fee_asset
        self._fee_account = fee_account
        self._treasury = treasury  # native-MNT fee recipient
        # Debit the fee from the USER's managed wallet (Opsi A): fee_payer(user_id,
        # to, amount) -> tx hash. None → fall back to the operator-paid native path.
        self._fee_payer = fee_payer

    def _on_shard(self, user_id: int) -> bool:
        return self._router is None or self._node is None or self._router.owns(user_id, self._node)

    async def create_grid(self, user_id: int, cfg: GridConfig, *, breaker=None,
                          monitor_interval: float = 0.0, profit_guard=None,
                          account_guard=None, timeframe: str = "1") -> str:
        session = await self._session(user_id)
        # SENSE the regime, then COMMIT the config hash on-chain BEFORE any order
        # rests. Commit-before-trade is what makes the record trustless (params can't
        # be fitted to results). If the chain is configured, a failed commit aborts
        # the launch — we never trade an uncommitted strategy.
        if self._chain is not None:
            regime = await classify_regime(session.exchange, cfg.market, self._signals,
                                           timeframe=timeframe)
            commit_tx = await self._chain.commit_strategy(cfg.instance_id, cfg)
            self._regimes[cfg.instance_id] = regime
            self._proofs[cfg.instance_id] = {"commit": commit_tx}
        await session.create(cfg, breaker=breaker, monitor_interval=monitor_interval,
                             profit_guard=profit_guard, account_guard=account_guard,
                             timeframe=timeframe)
        return cfg.instance_id

    def proofs(self, instance_id: str) -> dict:
        """On-chain tx hashes for an instance: {commit, attest, memory} (subset)."""
        return dict(self._proofs.get(instance_id, {}))

    async def recover(self) -> list[tuple[int, str]]:
        """Rebuild every open grid under its owning user from the store (replays
        PnL; no re-place). Call once on worker startup. Returns (user_id, instance_id)."""
        if self._store is None or not hasattr(self._store, "load_open_with_owner"):
            return []
        rebuilt: list[tuple[int, str]] = []
        for user_id, cfg in await self._store.load_open_with_owner():
            if not self._on_shard(user_id):
                continue  # another shard owns this user
            session = await self._session(user_id)
            await session.recover_instance(cfg)
            rebuilt.append((user_id, cfg.instance_id))
        return rebuilt

    async def stop_grid(self, user_id: int, instance_id: str) -> None:
        session = self._owning_session(user_id, instance_id)
        engine = session.manager.get(instance_id)  # grab the ref before stop tears it down
        await session.stop(instance_id)
        outcome = engine.outcome() if engine is not None else None
        # ATTEST the verified outcome + LEARN (write StrategyMemory). Post-trade writes
        # are fire-then-confirm in the chain client — they don't block the close path,
        # and a revert is logged, never raised (mirrors agent/loop.close_and_learn).
        if self._chain is not None and outcome is not None:
            try:
                self._proofs.setdefault(instance_id, {})["attest"] = await self._chain.attest(instance_id, outcome)
            except Exception as e:  # noqa: BLE001 — one failed write must not break the close
                print(f"  ! attest failed ({instance_id}): {e}")
            regime = self._regimes.pop(instance_id, None)
            if regime is not None:
                try:
                    self._proofs.setdefault(instance_id, {})["memory"] = await self._chain.write_memory(
                        to_record(regime, engine.cfg, outcome))
                except Exception as e:  # noqa: BLE001
                    print(f"  ! write_memory failed ({instance_id}): {e}")
        # Builder fee runs regardless of the chain (the user-wallet path is independent).
        await self._settle_builder_fee(user_id, instance_id)
        # Save the closed episode to history, then drop it from the active list so the
        # dashboard isn't cluttered with HALTED grids (the store still holds it).
        if outcome is not None and self._store is not None and hasattr(self._store, "record_episode"):
            try:
                await self._store.record_episode(user_id, instance_id, engine.cfg.market,
                                                 str(outcome.realized_pnl), outcome.fill_count, outcome.winrate)
            except Exception as e:  # noqa: BLE001
                print(f"  ! record_episode failed ({instance_id}): {e}")
        await session.forget(instance_id)

    async def _settle_builder_fee(self, user_id: int, instance_id: str) -> None:
        """Settle the flat builder fee on-chain. Precedence:
        1. user wallet (Opsi A): debit the fee from the USER's managed MNT wallet →
           treasury — the user pays, non-custodially funded from the faucet;
        2. ERC-20 Vault: from the operator's bond → treasury (Vault.settleFee);
        3. operator native MNT: the operator pays the fee straight to the treasury.
        A revert (insufficient funds, no wallet/bond) is logged and skipped — billing
        never blocks a clean close."""
        if self._builder_fee <= 0:
            return
        try:
            if self._fee_payer is not None and self._treasury:
                self._proofs.setdefault(instance_id, {})["fee"] = await self._fee_payer(
                    user_id, self._treasury, self._builder_fee)
            elif self._fee_asset and getattr(self._chain, "vault", None) is not None:
                payer = self._fee_account or getattr(getattr(self._chain, "acct", None), "address", "")
                if payer:
                    self._proofs.setdefault(instance_id, {})["fee"] = await self._chain.settle_fee(
                        payer, self._fee_asset, self._builder_fee)
            elif self._treasury and self._chain is not None and hasattr(self._chain, "send_native"):
                self._proofs.setdefault(instance_id, {})["fee"] = await self._chain.send_native(
                    self._treasury, self._builder_fee)
        except Exception as e:  # noqa: BLE001 — insufficient funds / revert is non-fatal
            print(f"  ! builder-fee settle skipped ({instance_id}): {e}")

    async def pause_grid(self, user_id: int, instance_id: str) -> None:
        self._owning_session(user_id, instance_id).pause(instance_id)

    async def clear_stopped(self, user_id: int) -> int:
        """Drop every HALTED/EXITING grid from the user's active list (history kept)."""
        session = self._sessions.get(user_id)
        return await session.clear_stopped() if session is not None else 0

    async def history(self, user_id: int, limit: int = 20) -> list[dict]:
        """Recently closed episodes for this user (pnl, fills, market, time, name)."""
        if self._store is None or not hasattr(self._store, "load_episodes"):
            return []
        rows = await self._store.load_episodes(user_id, limit)
        names = await self._grid_names(user_id)
        for r in rows:
            r["name"] = names.get(r["instance_id"], "")
        return rows

    async def status(self, user_id: int) -> Sequence[GridStatusView]:
        session = self._sessions.get(user_id)
        if session is None:
            return []
        names = await self._grid_names(user_id)
        return [
            GridStatusView(
                instance_id=eng.cfg.instance_id,
                state=eng.state.value,
                realized_pnl=str(eng.realized),
                fill_count=eng.fill_count,
                name=names.get(eng.cfg.instance_id, ""),
            )
            for eng in session.engines()
        ]

    async def _grid_names(self, user_id: int) -> dict:
        if self._store is not None and hasattr(self._store, "load_grid_names"):
            return await self._store.load_grid_names(user_id)
        return {}

    async def name_grid(self, user_id: int, instance_id: str, name: str) -> None:
        """Set a user-friendly name for one of the user's active grids."""
        session = self._sessions.get(user_id)
        if session is None or not session.has(instance_id):
            raise PermissionError("not your grid instance")
        if self._store is not None and hasattr(self._store, "set_grid_name"):
            await self._store.set_grid_name(user_id, instance_id, name)

    async def grid_detail(self, user_id: int, instance_id: str) -> dict | None:
        """Full view of one active grid: config + live state + name + on-chain proofs."""
        session = self._sessions.get(user_id)
        eng = session.manager.get(instance_id) if session is not None else None
        if eng is None:
            return None
        cfg = eng.cfg
        names = await self._grid_names(user_id)
        return {
            "instance_id": instance_id, "name": names.get(instance_id, ""),
            "market": cfg.market, "state": eng.state.value,
            "realized_pnl": str(eng.realized), "fill_count": eng.fill_count,
            "lower": str(cfg.lower), "upper": str(cfg.upper), "levels": cfg.levels,
            "order_size": str(cfg.order_size), "leverage": str(cfg.leverage),
            "bias": cfg.bias, "proofs": dict(self._proofs.get(instance_id, {})),
        }

    async def balance(self, user_id: int) -> BalanceView:
        session = await self._session(user_id)
        return await session.exchange.balance()

    async def market_info(self, user_id: int, market: str) -> MarketView:
        """Live top-of-book through the user's own venue client — lets a UI turn
        'band ±1%' into absolute grid bounds without importing any exchange SDK."""
        session = await self._session(user_id)
        bid, ask = await session.exchange.best_bid_ask(market)
        return MarketView(market=market, bid=str(bid), ask=str(ask), mid=str((bid + ask) / 2))

    async def disconnect(self, user_id: int) -> None:
        """Tear down a user's session (stop the consumer, close the client)."""
        session = self._sessions.pop(user_id, None)
        if session is not None:
            await session.aclose()

    async def drain(self) -> None:
        """Await in-flight on-chain confirmations — call on graceful worker shutdown
        so fire-then-confirm attest/write_memory txs land before the process exits."""
        if self._chain is not None and hasattr(self._chain, "drain"):
            await self._chain.drain()

    # ---- per-user strategy settings (validated upstream — app/prefs.py) ----
    # Durable via the store when it has settings methods; in-memory otherwise
    # (dev/demo and stores predating the user_settings table).

    async def get_settings(self, user_id: int) -> dict:
        if self._store is not None and hasattr(self._store, "get_settings"):
            raw = await self._store.get_settings(user_id)
            return json.loads(raw) if raw else {}
        return dict(self._prefs.get(user_id, {}))

    async def put_settings(self, user_id: int, settings: dict) -> None:
        if self._store is not None and hasattr(self._store, "put_settings"):
            await self._store.put_settings(user_id, json.dumps(settings))
        else:
            self._prefs[user_id] = dict(settings)

    async def reset_settings(self, user_id: int) -> None:
        if self._store is not None and hasattr(self._store, "delete_settings"):
            await self._store.delete_settings(user_id)
        else:
            self._prefs.pop(user_id, None)

    # ---- internals ----

    async def _session(self, user_id: int) -> UserSession:
        if not self._on_shard(user_id):
            raise PermissionError(f"user {user_id} is not on shard {self._node}")
        session = self._sessions.get(user_id)
        if session is None:
            client = self._client_factory(user_id) if self._client_factory else self._exchange
            if inspect.isawaitable(client):  # factory may be async (e.g. fetch+decrypt creds)
                client = await client
            session = UserSession(user_id, client, self._store)
            self._sessions[user_id] = session
        return session

    def _owning_session(self, user_id: int, instance_id: str) -> UserSession:
        session = self._sessions.get(user_id)
        if session is None or not session.has(instance_id):
            raise PermissionError("not your grid instance")
        return session
