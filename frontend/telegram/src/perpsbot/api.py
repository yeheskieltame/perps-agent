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

    async def create_grid(self, user_id: int, market: str, settings: dict | None = None,
                          template: str | None = None, margin: str | None = None,
                          margin_pct: str | None = None) -> dict:
        """Launch a grid. `settings` are per-launch knob overrides; `template`
        (safe/balanced/aggressive) sizes backend-side from `margin` (absolute USDT) or
        `margin_pct` (fraction of the user's free balance, the worker reads it).
        Returns {instance_id, effective, lower, upper}."""
        return await self._req("POST", "/v1/grids", user_id,
                               self._grid_body(market, settings, template, margin, margin_pct))

    async def preview_grid(self, user_id: int, market: str, template: str,
                           margin: str | None = None, margin_pct: str | None = None) -> dict:
        """Preview a template launch (no order placed): {size, notional, leverage,
        levels, lower, upper, margin, currency}. Lets the UI show the value first."""
        return await self._req("POST", "/v1/grids/preview", user_id,
                               self._grid_body(market, None, template, margin, margin_pct))

    @staticmethod
    def _grid_body(market, settings, template, margin, margin_pct) -> dict:
        body: dict = {"market": market}
        if settings:
            body["settings"] = settings
        if template:
            body["template"] = template
        if margin is not None:
            body["margin"] = str(margin)
        if margin_pct is not None:
            body["margin_pct"] = str(margin_pct)
        return body

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

    async def grid_detail(self, user_id: int, instance_id: str) -> dict:
        """Full view of one active grid: config + live state + name + on-chain proofs."""
        return await self._req("GET", f"/v1/grids/{instance_id}", user_id)

    async def rename_grid(self, user_id: int, instance_id: str, name: str) -> dict:
        return await self._req("PUT", f"/v1/grids/{instance_id}/name", user_id, {"name": name})

    async def stop(self, user_id: int, instance_id: str) -> dict:
        """Stop a grid. Returns {ok, proofs} — proofs carries the attest/memory tx
        hashes when the backend's verifiable loop is on-chain."""
        return await self._req("DELETE", f"/v1/grids/{instance_id}", user_id)

    async def pause(self, user_id: int, instance_id: str) -> None:
        await self._req("POST", f"/v1/grids/{instance_id}/pause", user_id)

    async def balance(self, user_id: int) -> dict:
        return await self._req("GET", "/v1/balance", user_id)

    async def positions(self, user_id: int) -> list[dict]:
        """Open Bybit positions: [{market, side, size, entry, mark, pnl, pnl_pct, notional}]."""
        return await self._req("GET", "/v1/positions", user_id)

    async def open_orders(self, user_id: int) -> list[dict]:
        """Resting venue orders: [{market, side, price, qty, level}]."""
        return await self._req("GET", "/v1/orders", user_id)

    async def close_position(self, user_id: int, market: str) -> dict:
        return await self._req("POST", f"/v1/positions/{market}/close", user_id)

    async def close_all_positions(self, user_id: int) -> dict:
        return await self._req("POST", "/v1/positions/close-all", user_id)

    async def cancel_all_orders(self, user_id: int) -> dict:
        """Cancel every resting order (also stops grids so they don't re-place)."""
        return await self._req("POST", "/v1/orders/cancel-all", user_id)

    async def panic(self, user_id: int) -> dict:
        """Flat & out: stop all grids, cancel all orders, close all positions."""
        return await self._req("POST", "/v1/panic", user_id)

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
