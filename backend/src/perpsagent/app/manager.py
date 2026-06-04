"""GridManager — supervises one asyncio task per grid instance.

`create` places the grid synchronously (await start) then spawns the fill
consumer, so callers know the grid is live on return (no startup race).
"""
from __future__ import annotations

import asyncio

from ..domain.models import GridConfig
from .engine import GridEngine


class GridManager:
    def __init__(self) -> None:
        self._engines: dict[str, GridEngine] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def create(self, exchange, cfg: GridConfig, store=None) -> GridEngine:
        engine = GridEngine(exchange, cfg, store)
        await engine.start()
        if store is not None and hasattr(store, "save_instance"):
            await store.save_instance(cfg)
        self._engines[cfg.instance_id] = engine
        self._tasks[cfg.instance_id] = asyncio.create_task(engine.consume())
        return engine

    async def stop(self, instance_id: str) -> None:
        engine = self._engines.get(instance_id)
        if engine is not None:
            await engine.stop()
            if engine.store is not None and hasattr(engine.store, "set_state"):
                await engine.store.set_state(instance_id, "CLOSED")
        task = self._tasks.pop(instance_id, None)
        if task is not None:
            task.cancel()

    async def recover(self, store, exchange) -> list[GridEngine]:
        """Rebuild engines for open instances from persisted state (replays PnL).
        Does NOT re-place orders; live resume needs venue reconciliation (roadmap)."""
        out: list[GridEngine] = []
        for cfg in await store.load_open_instances():
            engine = GridEngine(exchange, cfg, store)
            engine.rehydrate(await store.load_fills(cfg.instance_id))
            self._engines[cfg.instance_id] = engine
            out.append(engine)
        return out

    def pause(self, instance_id: str) -> None:
        engine = self._engines.get(instance_id)
        if engine is not None:
            engine.pause()

    def get(self, instance_id: str) -> GridEngine | None:
        return self._engines.get(instance_id)

    def all(self) -> list[GridEngine]:
        return list(self._engines.values())
