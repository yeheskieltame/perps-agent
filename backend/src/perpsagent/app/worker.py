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
  GET    /v1/settings             -> {settings: {band, levels, size, leverage, ...}, customized: [...]}
  PUT    /v1/settings             {key: value, ...} -> validated, persisted per user
  DELETE /v1/settings             -> reset to defaults
  GET    /v1/balance              -> {equity, available, currency}
  GET    /v1/market/{market}      -> {market, bid, ask, mid}
  PUT    /v1/credentials          {api_key, api_secret, testnet=true} -> {ok, testnet}
  GET    /v1/credentials          -> {connected, testnet, key_preview} | {connected: false}
  DELETE /v1/credentials          -> {ok}  (forgets the keys, tears down the session)

A request for a user this shard does not own returns 409 (the gateway should never
send one — this is defense-in-depth). A user with no stored venue keys gets 401 on
any endpoint that needs their exchange client. The verifiable loop runs through the
GridService facade: launch COMMITs the config hash on-chain before trading, stop
ATTESTs the outcome + writes StrategyMemory (see app/service.py). SKETCH: still no
auth on X-User-Id — the gateway is expected to authenticate; see TODOs.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from aiohttp import web

from ..domain.models import GridConfig, Spacing, Venue
from . import prefs
from .service import AppService


def _user_id(request: web.Request) -> int:
    raw = request.headers.get("X-User-Id")
    if raw is None:
        raise web.HTTPBadRequest(reason="missing X-User-Id header")
    try:
        return int(raw)
    except ValueError:
        raise web.HTTPBadRequest(reason="X-User-Id must be an integer") from None


def _cfg_from_body(body: dict, node: str, knobs: dict) -> GridConfig:
    """Grid config = transport fields from the body + tunables from the user's
    merged knobs (defaults < saved settings < body["settings"] overrides).
    Legacy top-level levels/order_size/leverage (the pre-settings API) still win."""
    try:
        fields = prefs.grid_fields(knobs)
        if "levels" in body:
            fields["levels"] = int(body["levels"])
        if "order_size" in body:
            fields["order_size"] = Decimal(str(body["order_size"]))
        if "leverage" in body:
            fields["leverage"] = Decimal(str(body["leverage"]))
        return GridConfig(
            instance_id=f"{body['market']}-{node}-{uuid.uuid4().hex[:8]}",
            venue=Venue(body.get("venue", "bybit")),
            market=body["market"],
            lower=Decimal(str(body["lower"])),
            upper=Decimal(str(body["upper"])),
            spacing=Spacing(body.get("spacing", "geometric")),
            **fields,
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
        # Merge the user's tunables: defaults < saved settings < body["settings"].
        try:
            overrides = prefs.validate_updates(body.get("settings") or {})
        except ValueError as e:
            raise web.HTTPBadRequest(reason=str(e)) from None
        knobs = prefs.merged(await service.get_settings(user_id), overrides)
        # Bounds: explicit lower/upper > legacy {band: fraction} > knob band (%).
        # The worker resolves band bounds from the user's own live top-of-book, so
        # the UI never needs an exchange SDK (the GridService seam stays the boundary).
        if not ("lower" in body or "upper" in body):
            try:
                market = body["market"]
                if "band" in body:  # legacy shorthand: fraction in (0, 1)
                    band = Decimal(str(body["band"]))
                    if not Decimal(0) < band < Decimal(1):
                        raise ValueError("band must be a fraction in (0, 1)")
                else:
                    band = prefs.band_fraction(knobs)
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
        cfg = _cfg_from_body(body, node, knobs)
        breaker, profit_guard, account_guard = prefs.build_guards(knobs)
        try:
            iid = await service.create_grid(
                user_id, cfg, breaker=breaker, profit_guard=profit_guard,
                account_guard=account_guard, timeframe=prefs.timeframe_code(knobs),
                monitor_interval=prefs.monitor_interval(knobs),
            )
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"instance_id": iid, "effective": knobs,
                                  "lower": str(cfg.lower), "upper": str(cfg.upper),
                                  "proofs": service.proofs(iid)})

    async def get_settings(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        saved = await service.get_settings(user_id)
        return web.json_response({"settings": prefs.merged(saved),
                                  "customized": sorted(k for k in saved if k in prefs.KNOBS)})

    async def put_settings(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        body = await request.json()
        try:
            updates = prefs.validate_updates(body)
        except ValueError as e:
            raise web.HTTPBadRequest(reason=str(e)) from None
        if not updates:
            raise web.HTTPBadRequest(reason="no settings given")
        saved = {**await service.get_settings(user_id), **updates}
        await service.put_settings(user_id, saved)
        return web.json_response({"settings": prefs.merged(saved), "updated": sorted(updates)})

    async def delete_settings(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        await service.reset_settings(user_id)
        return web.json_response({"settings": prefs.merged(None)})

    async def stop(request: web.Request) -> web.Response:
        instance_id = request.match_info["instance_id"]
        try:
            await service.stop_grid(_user_id(request), instance_id)
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"ok": True, "proofs": service.proofs(instance_id)})

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
    app.router.add_get("/v1/settings", get_settings)
    app.router.add_put("/v1/settings", put_settings)
    app.router.add_delete("/v1/settings", delete_settings)
    app.router.add_delete("/v1/grids/{instance_id}", stop)
    app.router.add_post("/v1/grids/{instance_id}/pause", pause)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/balance", balance)
    app.router.add_get("/v1/market/{market}", market)
    app.router.add_put("/v1/credentials", put_credentials)
    app.router.add_get("/v1/credentials", get_credentials)
    app.router.add_delete("/v1/credentials", delete_credentials)
    return app


def _chain_from_settings(s):
    """Real Mantle client when fully configured (RPC + key + ledger + memory addr),
    else None → proofs disabled (dev/demo). The worker's verifiable loop signs every
    proof with the OPERATOR wallet; users stay non-custodial on their own venue keys."""
    if s.mantle_rpc and s.mantle_private_key and s.strategy_ledger_addr and s.strategy_memory_addr:
        from ..adapters.chain.client import MantleChainClient

        return MantleChainClient(
            rpc_url=s.mantle_rpc, private_key=s.mantle_private_key,
            ledger_addr=s.strategy_ledger_addr, memory_addr=s.strategy_memory_addr,
            vault_addr=s.vault_addr or None, detail_path=s.memory_detail_path or None,
        )
    return None


def _signals_from_settings(s) -> list:
    """SENSE inputs — each signal added only when its key is set (regime fails soft
    to vol-only otherwise). Same fan-out the CLI runner wires."""
    signals: list = []
    if s.elfa_api_key:
        from ..adapters.signals.elfa import ElfaSignals

        signals.append(ElfaSignals({"api_key": s.elfa_api_key}))
    if s.nansen_api_key:
        from ..adapters.signals.nansen import NansenSignals

        signals.append(NansenSignals({"api_key": s.nansen_api_key}))
    if s.surf_api_key:
        from ..adapters.signals.surf import SurfSignals

        signals.append(SurfSignals({"api_key": s.surf_api_key, "base_url": s.surf_base_url}))
    return signals


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
    service = AppService(store=store, client_factory=factory, router=router, node=node,
                         chain=_chain_from_settings(s), signals=_signals_from_settings(s),
                         builder_fee=s.builder_fee, fee_asset=s.fee_asset,
                         fee_account=s.fee_account, treasury=s.x402_pay_to)
    return service, creds_admin


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

    async def _cleanup(_app):
        await service.drain()  # let fire-then-confirm attest/memory writes land

    app = build_worker_app(service, s.shard_node, creds=creds_admin)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    print(f"[worker {s.shard_node}/{s.shard_count}] serving on {s.worker_host}:{s.worker_port}")
    web.run_app(app, host=s.worker_host, port=s.worker_port)


if __name__ == "__main__":
    main()
