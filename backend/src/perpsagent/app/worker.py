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
                                  (or {market, band, levels, ...} — bounds = mid*(1±band))
  DELETE /v1/grids/{instance_id}
  POST   /v1/grids/{instance_id}/pause
  GET    /v1/status               -> [{instance_id, state, realized_pnl, fill_count}]
  GET    /v1/balance              -> {equity, available, currency}
  GET    /v1/market/{market}      -> {market, bid, ask, mid}
  PUT    /v1/credentials          {api_key, api_secret, testnet=true} -> {ok, testnet}
  GET    /v1/credentials          -> {connected, testnet, key_preview} | {connected: false}
  DELETE /v1/credentials          -> {ok}  (forgets the keys, tears down the session)

A request for a user this shard does not own returns 409 (the gateway should never
send one — this is defense-in-depth). A user with no stored venue keys gets 401 on
any endpoint that needs their exchange client. SKETCH: no auth on X-User-Id, and
grids run through the GridService facade directly (the verifiable LearningLoop
commit/attest is a follow-up); see TODOs.
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


_NO_CREDS = "no venue credentials — connect your API keys first"


def build_worker_app(service: AppService, node: str, creds=None) -> web.Application:
    app = web.Application()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "node": node})

    async def create(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        body = await request.json()
        # Band shorthand: a UI may send {band: 0.01} instead of absolute bounds —
        # the worker resolves them from the user's own live top-of-book, so the
        # UI never needs an exchange SDK (the GridService seam stays the boundary).
        if "band" in body and not ("lower" in body or "upper" in body):
            try:
                market = body["market"]
                band = Decimal(str(body["band"]))
                if not Decimal(0) < band < Decimal(1):
                    raise ValueError("band must be a fraction in (0, 1)")
            except (KeyError, ValueError, ArithmeticError) as e:
                raise web.HTTPBadRequest(reason=f"bad grid config: {e}") from None
            try:
                m = await service.market_info(user_id, market)
            except KeyError:
                raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
            except PermissionError as e:
                raise web.HTTPConflict(reason=str(e)) from None
            mid = Decimal(m.mid)
            body["lower"], body["upper"] = str(mid * (1 - band)), str(mid * (1 + band))
        cfg = _cfg_from_body(body, node)
        try:
            iid = await service.create_grid(user_id, cfg)
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
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
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"equity": str(b.equity), "available": str(b.available), "currency": b.currency})

    async def market(request: web.Request) -> web.Response:
        try:
            m = await service.market_info(_user_id(request), request.match_info["market"])
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"market": m.market, "bid": m.bid, "ask": m.ask, "mid": m.mid})

    def _creds_or_503():
        if creds is None:
            raise web.HTTPServiceUnavailable(
                reason="credential storage not configured — set PERPSAGENT_CRED_MASTER_KEY")
        return creds

    async def put_credentials(request: web.Request) -> web.Response:
        admin = _creds_or_503()
        user_id = _user_id(request)
        body = await request.json()
        api_key = str(body.get("api_key", "")).strip()
        api_secret = str(body.get("api_secret", "")).strip()
        if not api_key or not api_secret:
            raise web.HTTPBadRequest(reason="api_key and api_secret are required")
        testnet = bool(body.get("testnet", True))  # testnet-first: mainnet is opt-in
        await admin.put(user_id, api_key, api_secret, testnet)
        await service.disconnect(user_id)  # next request rebuilds the client on the new keys
        return web.json_response({"ok": True, "testnet": testnet})

    async def get_credentials(request: web.Request) -> web.Response:
        admin = _creds_or_503()
        info = await admin.info(_user_id(request))
        return web.json_response(info or {"connected": False})

    async def delete_credentials(request: web.Request) -> web.Response:
        admin = _creds_or_503()
        user_id = _user_id(request)
        await admin.delete(user_id)
        await service.disconnect(user_id)
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/grids", create)
    app.router.add_delete("/v1/grids/{instance_id}", stop)
    app.router.add_post("/v1/grids/{instance_id}/pause", pause)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/balance", balance)
    app.router.add_get("/v1/market/{market}", market)
    app.router.add_put("/v1/credentials", put_credentials)
    app.router.add_get("/v1/credentials", get_credentials)
    app.router.add_delete("/v1/credentials", delete_credentials)
    return app


def _service_from_settings(s, router, node: str):
    """Wire store + per-user client factory from config. Returns (service, creds_admin).

    Store: Postgres when a DSN is set, else SQLite — both implement
    CredentialStorePort, so per-user encrypted keys work without Postgres.
    Clients: with PERPSAGENT_CRED_MASTER_KEY set, each user gets their OWN
    BybitExchange built from their sealed keys (the product path); without it,
    a shared in-memory FakeExchange (dev/demo only).
    """
    creds_admin = None
    factory = None
    if s.postgres_dsn:
        from ..adapters.store.postgres_store import PostgresStore

        store = PostgresStore(s.postgres_dsn)
    else:
        from ..adapters.store.sqlite_store import SqliteStore

        store = SqliteStore(s.store_db_path)
    if s.cred_master_key:  # per-user encrypted keys → per-user Bybit client
        from ..adapters.exchanges.bybit.adapter import BybitExchange
        from ..adapters.store.credentials import (
            CredentialAdmin, CredentialCodec, credential_client_factory,
        )

        codec = CredentialCodec(s.cred_master_key)
        factory = credential_client_factory(
            store, codec,
            lambda creds: BybitExchange({**creds, "rate_limit": s.bybit_rate_limit,
                                         "max_retries": s.bybit_max_retries}),
        )
        creds_admin = CredentialAdmin(store, codec)
    if factory is None:  # dev/demo fallback: in-memory venue, no keys
        from ..adapters.exchanges.fake import FakeExchange

        factory = lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"})  # noqa: E731
    return AppService(store=store, client_factory=factory, router=router, node=node), creds_admin


def main() -> None:
    from ..config import Settings
    from .shard import ShardRouter

    s = Settings()
    s.assert_consistent()
    router = ShardRouter(s.shard_count)
    service, creds_admin = _service_from_settings(s, router, s.shard_node)

    async def _startup(_app):
        recovered = await service.recover()
        print(f"[worker {s.shard_node}] recovered {len(recovered)} instance(s)")

    app = build_worker_app(service, s.shard_node, creds=creds_admin)
    app.on_startup.append(_startup)
    print(f"[worker {s.shard_node}/{s.shard_count}] serving on :{s.worker_port}")
    web.run_app(app, port=s.worker_port)


if __name__ == "__main__":
    main()
