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

    async def create_grid(self, user_id: int, market: str,
                          settings: dict | None = None) -> dict:
        """Launch a grid. `settings` are per-launch knob overrides (band in %,
        levels, size, leverage, tp, ... — validated backend-side); everything not
        given comes from the user's saved settings, then defaults. Returns the
        full response: {instance_id, effective, lower, upper}."""
        body: dict = {"market": market}
        if settings:
            body["settings"] = settings
        return await self._req("POST", "/v1/grids", user_id, body)

    async def get_settings(self, user_id: int) -> dict:
        return await self._req("GET", "/v1/settings", user_id)

    async def put_settings(self, user_id: int, updates: dict) -> dict:
        return await self._req("PUT", "/v1/settings", user_id, updates)

    async def reset_settings(self, user_id: int) -> dict:
        return await self._req("DELETE", "/v1/settings", user_id)

    async def status(self, user_id: int) -> list[dict]:
        return await self._req("GET", "/v1/status", user_id)

    async def clear_stopped(self, user_id: int) -> dict:
        """Drop HALTED/EXITING grids from the active list (history kept). -> {cleared}"""
        return await self._req("POST", "/v1/grids/clear", user_id)

    async def history(self, user_id: int) -> list[dict]:
        """Recently closed grids: [{instance_id, market, realized_pnl, fill_count, winrate, closed_at}]."""
        return await self._req("GET", "/v1/history", user_id)

    async def stop(self, user_id: int, instance_id: str) -> dict:
        """Stop a grid. Returns {ok, proofs} — proofs carries the attest/memory tx
        hashes when the backend's verifiable loop is on-chain."""
        return await self._req("DELETE", f"/v1/grids/{instance_id}", user_id)

    async def pause(self, user_id: int, instance_id: str) -> None:
        await self._req("POST", f"/v1/grids/{instance_id}/pause", user_id)

    async def balance(self, user_id: int) -> dict:
        return await self._req("GET", "/v1/balance", user_id)

    async def wallet(self, user_id: int) -> dict:
        """The user's managed MNT wallet (minted on first call): {address, balance,
        currency, faucet}. Pays the builder fee on Mantle."""
        return await self._req("GET", "/v1/wallet", user_id)

    async def put_credentials(self, user_id: int, api_key: str, api_secret: str,
                              testnet: bool = True) -> dict:
        return await self._req("PUT", "/v1/credentials", user_id,
                               {"api_key": api_key, "api_secret": api_secret,
                                "testnet": testnet})

    async def get_credentials(self, user_id: int) -> dict:
        return await self._req("GET", "/v1/credentials", user_id)

    async def delete_credentials(self, user_id: int) -> None:
        await self._req("DELETE", "/v1/credentials", user_id)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
