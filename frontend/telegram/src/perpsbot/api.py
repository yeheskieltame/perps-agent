"""WorkerAPI — the bot's ONLY line to the backend: the worker/gateway HTTP API
(backend/README.md §1). Identity = `X-User-Id` (the Telegram user id). The bot
never imports the engine, adapters, or any exchange/chain SDK.
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Non-200 from the backend. `status` + the backend's reason text."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class WorkerAPI:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._session = None  # lazy aiohttp.ClientSession

    async def _req(self, method: str, path: str, user_id: int | None = None,
                   body: dict | None = None) -> Any:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession()
        headers = {"X-User-Id": str(user_id)} if user_id is not None else {}
        async with self._session.request(method, self._base + path, json=body,
                                         headers=headers) as r:
            if r.status != 200:
                raise ApiError(r.status, (await r.text())[:200].strip())
            return await r.json()

    async def health(self) -> dict:
        return await self._req("GET", "/healthz")

    async def market(self, user_id: int, market: str) -> dict:
        return await self._req("GET", f"/v1/market/{market}", user_id)

    async def create_grid(self, user_id: int, market: str, band: str, levels: int,
                          size: str, leverage: str = "1") -> str:
        body = {"market": market, "band": band, "levels": levels,
                "order_size": size, "leverage": leverage}
        return (await self._req("POST", "/v1/grids", user_id, body))["instance_id"]

    async def status(self, user_id: int) -> list[dict]:
        return await self._req("GET", "/v1/status", user_id)

    async def stop(self, user_id: int, instance_id: str) -> None:
        await self._req("DELETE", f"/v1/grids/{instance_id}", user_id)

    async def pause(self, user_id: int, instance_id: str) -> None:
        await self._req("POST", f"/v1/grids/{instance_id}/pause", user_id)

    async def balance(self, user_id: int) -> dict:
        return await self._req("GET", "/v1/balance", user_id)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
