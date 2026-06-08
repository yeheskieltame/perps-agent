"""Worker entrypoint — serves ONE shard of the engine plane over HTTP.

One worker process = one event loop = one shard (plan/SCALING.md #10). It owns the
users `ShardRouter` assigns to its node, holds their sessions / fill streams /
exchange clients, and exposes the `GridService` facade over HTTP so the gateway
(and UI) reach it the same way regardless of which worker a user lands on. Run N of
these with distinct `PERPSAGENT_SHARD_NODE` behind `app/gateway.py`.

  PERPSAGENT_SHARD_NODE=0 PERPSAGENT_SHARD_COUNT=2 PERPSAGENT_WORKER_PORT=9000 \
    python -m perpsagent.app.worker

API (user identified by the `X-User-Id` header):
  GET    /healthz
  POST   /v1/grids                {market, lower, upper, levels, order_size, ...} -> {instance_id}
  DELETE /v1/grids/{instance_id}
  POST   /v1/grids/{instance_id}/pause
  GET    /v1/status               -> [{instance_id, state, realized_pnl, fill_count}]
  GET    /v1/balance              -> {equity, available, currency}

A request for a user this shard does not own returns 409 (the gateway should never
send one — this is defense-in-depth). SKETCH: no auth on X-User-Id, and grids run
through the GridService facade directly (the verifiable LearningLoop commit/attest
is a follow-up); see TODOs.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from aiohttp import web

from ..domain.models import GridConfig, Spacing, Venue
from .service import AppService


def _user_id(request: web.Request) -> int:
    raw = request.headers.get("X-User-Id")
    if raw is None:
        raise web.HTTPBadRequest(reason="missing X-User-Id header")
    try:
        return int(raw)
    except ValueError:
        raise web.HTTPBadRequest(reason="X-User-Id must be an integer") from None


def _cfg_from_body(body: dict, node: str) -> GridConfig:
    try:
        return GridConfig(
            instance_id=f"{body['market']}-{node}-{uuid.uuid4().hex[:8]}",
            venue=Venue(body.get("venue", "bybit")),
            market=body["market"],
            lower=Decimal(str(body["lower"])),
            upper=Decimal(str(body["upper"])),
            levels=int(body["levels"]),
            order_size=Decimal(str(body.get("order_size", "0.01"))),
            spacing=Spacing(body.get("spacing", "geometric")),
            leverage=Decimal(str(body.get("leverage", "1"))),
        )
    except (KeyError, ValueError) as e:
        raise web.HTTPBadRequest(reason=f"bad grid config: {e}") from None


def build_worker_app(service: AppService, node: str) -> web.Application:
    app = web.Application()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "node": node})

    async def create(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        cfg = _cfg_from_body(await request.json(), node)
        try:
            iid = await service.create_grid(user_id, cfg)
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"instance_id": iid})

    async def stop(request: web.Request) -> web.Response:
        try:
            await service.stop_grid(_user_id(request), request.match_info["instance_id"])
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"ok": True})

    async def pause(request: web.Request) -> web.Response:
        try:
            await service.pause_grid(_user_id(request), request.match_info["instance_id"])
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"ok": True})

    async def status(request: web.Request) -> web.Response:
        rows = await service.status(_user_id(request))
        return web.json_response([
            {"instance_id": s.instance_id, "state": s.state,
             "realized_pnl": s.realized_pnl, "fill_count": s.fill_count}
            for s in rows
        ])

    async def balance(request: web.Request) -> web.Response:
        try:
            b = await service.balance(_user_id(request))
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"equity": str(b.equity), "available": str(b.available), "currency": b.currency})

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/grids", create)
    app.router.add_delete("/v1/grids/{instance_id}", stop)
    app.router.add_post("/v1/grids/{instance_id}/pause", pause)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/balance", balance)
    return app


def _service_from_settings(s, router, node: str) -> AppService:
    """Wire the per-user client factory + durable store from config (best-effort)."""
    store = None
    factory = None
    if s.postgres_dsn:
        from ..adapters.store.postgres_store import PostgresStore

        store = PostgresStore(s.postgres_dsn)
        if s.cred_master_key:  # per-user encrypted keys → per-user Bybit client
            from ..adapters.exchanges.bybit.adapter import BybitExchange
            from ..adapters.store.credentials import CredentialCodec, credential_client_factory

            codec = CredentialCodec(s.cred_master_key)
            factory = credential_client_factory(
                store, codec,
                lambda creds: BybitExchange({**creds, "rate_limit": s.bybit_rate_limit,
                                             "max_retries": s.bybit_max_retries}),
            )
    if factory is None:  # dev/demo fallback: in-memory venue, no keys
        from ..adapters.exchanges.fake import FakeExchange

        factory = lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"})  # noqa: E731
    return AppService(store=store, client_factory=factory, router=router, node=node)


def main() -> None:
    from ..config import Settings
    from .shard import ShardRouter

    s = Settings()
    s.assert_consistent()
    router = ShardRouter(s.shard_count)
    service = _service_from_settings(s, router, s.shard_node)

    async def _startup(_app):
        recovered = await service.recover()
        print(f"[worker {s.shard_node}] recovered {len(recovered)} instance(s)")

    app = build_worker_app(service, s.shard_node)
    app.on_startup.append(_startup)
    print(f"[worker {s.shard_node}/{s.shard_count}] serving on :{s.worker_port}")
    web.run_app(app, port=s.worker_port)


if __name__ == "__main__":
    main()
