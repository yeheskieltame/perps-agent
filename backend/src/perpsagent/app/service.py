"""GridService — the ONLY seam the UI (Telegram / web) may depend on.

The bot must NEVER import the engine, adapters, or any exchange/chain SDK. Keep
this facade small, typed, and versioned (mirrors deltaperps).
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from ..domain.models import BalanceView, GridConfig
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
                 router=None, node=None) -> None:
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

    def _on_shard(self, user_id: int) -> bool:
        return self._router is None or self._node is None or self._router.owns(user_id, self._node)

    async def create_grid(self, user_id: int, cfg: GridConfig, *, breaker=None,
                          monitor_interval: float = 0.0, profit_guard=None,
                          account_guard=None, timeframe: str = "1") -> str:
        session = await self._session(user_id)
        await session.create(cfg, breaker=breaker, monitor_interval=monitor_interval,
                             profit_guard=profit_guard, account_guard=account_guard,
                             timeframe=timeframe)
        return cfg.instance_id

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
        await self._owning_session(user_id, instance_id).stop(instance_id)

    async def pause_grid(self, user_id: int, instance_id: str) -> None:
        self._owning_session(user_id, instance_id).pause(instance_id)

    async def status(self, user_id: int) -> Sequence[GridStatusView]:
        session = self._sessions.get(user_id)
        if session is None:
            return []
        return [
            GridStatusView(
                instance_id=eng.cfg.instance_id,
                state=eng.state.value,
                realized_pnl=str(eng.realized),
                fill_count=eng.fill_count,
            )
            for eng in session.engines()
        ]

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
