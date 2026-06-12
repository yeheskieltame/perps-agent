"""UserSession — one user's exchange client + the single fill-stream consumer that
routes fills to that user's engines.

This is what makes the engine multi-tenant (plan/SCALING.md #6):
- each user trades on THEIR OWN venue keys (non-custodial; capital isolation);
- ONE private fill stream per user, consumed once and routed by `instance_id` to
  the owning engine. The old path had every engine call `stream_fills()` itself,
  so M engines on one account meant M streams and every fill processed M times
  (O(N^2) fan-out). Here a user's grids share one stream and one consumer.

State is in-process (per worker). Durable ownership/recovery across restarts is
the next step (Postgres, StorePort) — see plan/SCALING.md #8.
"""
from __future__ import annotations

import asyncio

from ..domain.models import GridConfig
from .engine import GridEngine
from .manager import GridManager


class UserSession:
    def __init__(self, user_id: int, exchange, store=None) -> None:
        self.user_id = user_id
        self.exchange = exchange
        self.store = store
        self.manager = GridManager()
        self._router: asyncio.Task | None = None

    async def create(self, cfg: GridConfig, *, breaker=None, monitor_interval: float = 0.0,
                     profit_guard=None, account_guard=None, timeframe: str = "1") -> GridEngine:
        engine = await self.manager.create(
            self.exchange, cfg, self.store, breaker=breaker,
            monitor_interval=monitor_interval, profit_guard=profit_guard, consume=False,
            account_guard=account_guard, timeframe=timeframe,
        )
        if self.store is not None and hasattr(self.store, "set_owner"):
            await self.store.set_owner(cfg.instance_id, self.user_id)  # durable ownership
        self._ensure_router()  # start the shared consumer on the first grid
        return engine

    async def recover_instance(self, cfg: GridConfig) -> GridEngine:
        """Rebuild one persisted grid: replay its fills (restores realized PnL /
        inventory) and register it under this user's consumer. Does NOT re-place
        orders — that needs venue reconciliation (roadmap, mirrors GridManager)."""
        engine = GridEngine(self.exchange, cfg, self.store)
        if self.store is not None and hasattr(self.store, "load_fills"):
            engine.rehydrate(await self.store.load_fills(cfg.instance_id))
        self.manager.register(engine)
        self._ensure_router()
        return engine

    def has(self, instance_id: str) -> bool:
        return self.manager.get(instance_id) is not None

    def engines(self) -> list[GridEngine]:
        return self.manager.all()

    async def stop(self, instance_id: str) -> None:
        await self.manager.stop(instance_id)
        if not self.manager.all():  # last grid gone — tear down the idle consumer
            await self._cancel_router()

    def pause(self, instance_id: str) -> None:
        self.manager.pause(instance_id)

    async def aclose(self) -> None:
        """Stop the consumer and close the venue client (call on user disconnect)."""
        await self._cancel_router()
        close = getattr(self.exchange, "close", None)
        if close is not None:
            await close()

    # ---- internals ----

    def _ensure_router(self) -> None:
        if self._router is None or self._router.done():
            self._router = asyncio.create_task(self._route_fills())

    async def _route_fills(self) -> None:
        async for fill in self.exchange.stream_fills():
            engine = self.manager.get(fill.instance_id)
            if engine is not None:  # ignore fills for instances this session doesn't own
                await engine.handle_fill(fill)

    async def _cancel_router(self) -> None:
        if self._router is not None:
            self._router.cancel()
            try:
                await self._router
            except BaseException:  # noqa: BLE001 — teardown: swallow cancel/stream errors
                pass
            self._router = None
