"""GridService — the ONLY seam the UI (Telegram / web) may depend on.

The bot must NEVER import the engine, adapters, or any exchange/chain SDK. Keep
this facade small, typed, and versioned (mirrors deltaperps). See repo CLAUDE.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..domain.models import BalanceView, GridConfig
from .manager import GridManager


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
    """Concrete GridService over a single venue client (one account)."""

    def __init__(self, exchange, store=None) -> None:
        self._exchange = exchange
        self._store = store
        self._manager = GridManager()
        self._owner: dict[str, int] = {}  # instance_id -> user_id

    async def create_grid(self, user_id: int, cfg: GridConfig) -> str:
        await self._manager.create(self._exchange, cfg, self._store)
        self._owner[cfg.instance_id] = user_id
        return cfg.instance_id

    async def stop_grid(self, user_id: int, instance_id: str) -> None:
        self._assert_owner(user_id, instance_id)
        await self._manager.stop(instance_id)

    async def pause_grid(self, user_id: int, instance_id: str) -> None:
        self._assert_owner(user_id, instance_id)
        self._manager.pause(instance_id)

    async def status(self, user_id: int) -> Sequence[GridStatusView]:
        out: list[GridStatusView] = []
        for eng in self._manager.all():
            if self._owner.get(eng.cfg.instance_id) != user_id:
                continue
            out.append(
                GridStatusView(
                    instance_id=eng.cfg.instance_id,
                    state=eng.state.value,
                    realized_pnl=str(eng.realized),
                    fill_count=eng.fill_count,
                )
            )
        return out

    async def balance(self, user_id: int) -> BalanceView:
        return await self._exchange.balance()

    def _assert_owner(self, user_id: int, instance_id: str) -> None:
        if self._owner.get(instance_id) != user_id:
            raise PermissionError("not your grid instance")
