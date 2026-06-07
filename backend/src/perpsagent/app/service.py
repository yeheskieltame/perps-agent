"""GridService — the ONLY seam the UI (Telegram / web) may depend on.

The bot must NEVER import the engine, adapters, or any exchange/chain SDK. Keep
this facade small, typed, and versioned (mirrors deltaperps).
"""
from __future__ import annotations

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


class GridService(Protocol):
    async def create_grid(self, user_id: int, cfg: GridConfig) -> str: ...
    async def stop_grid(self, user_id: int, instance_id: str) -> None: ...
    async def pause_grid(self, user_id: int, instance_id: str) -> None: ...
    async def status(self, user_id: int) -> Sequence[GridStatus]: ...
    async def balance(self, user_id: int) -> BalanceView: ...


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
                 client_factory: Callable[[int], object] | None = None) -> None:
        if exchange is None and client_factory is None:
            raise ValueError("AppService needs an `exchange` or a `client_factory`")
        self._exchange = exchange
        self._store = store
        self._client_factory = client_factory
        self._sessions: dict[int, UserSession] = {}

    async def create_grid(self, user_id: int, cfg: GridConfig) -> str:
        await self._session(user_id).create(cfg)
        return cfg.instance_id

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
        return await self._session(user_id).exchange.balance()

    async def disconnect(self, user_id: int) -> None:
        """Tear down a user's session (stop the consumer, close the client)."""
        session = self._sessions.pop(user_id, None)
        if session is not None:
            await session.aclose()

    # ---- internals ----

    def _session(self, user_id: int) -> UserSession:
        session = self._sessions.get(user_id)
        if session is None:
            client = self._client_factory(user_id) if self._client_factory else self._exchange
            session = UserSession(user_id, client, self._store)
            self._sessions[user_id] = session
        return session

    def _owning_session(self, user_id: int, instance_id: str) -> UserSession:
        session = self._sessions.get(user_id)
        if session is None or not session.has(instance_id):
            raise PermissionError("not your grid instance")
        return session
