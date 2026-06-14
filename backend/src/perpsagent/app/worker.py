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
  POST   /v1/grids/clear           -> {cleared: N}  (drop HALTED/EXITING from the active list)
  GET    /v1/history               -> [{instance_id, market, realized_pnl, fill_count, winrate, closed_at}]
  GET    /v1/settings             -> {settings: {band, levels, size, leverage, ...}, customized: [...]}
  PUT    /v1/settings             {key: value, ...} -> validated, persisted per user
  DELETE /v1/settings             -> reset to defaults
  GET    /v1/balance              -> {equity, available, currency}
  GET    /v1/market/{market}      -> {market, bid, ask, mid}
  PUT    /v1/credentials          {api_key, api_secret, testnet=true} -> {ok, testnet}
  GET    /v1/credentials          -> {connected, testnet, key_preview} | {connected: false}
  DELETE /v1/credentials          -> {ok}  (forgets the keys, tears down the session)
  GET    /v1/wallet               -> {address, balance, currency, faucet}  (managed MNT
                                  wallet, minted on first call; pays the builder fee)

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
_FAUCET_URL = "https://faucet.sepolia.mantle.xyz"  # Mantle Sepolia testnet MNT faucet


def build_worker_app(service: AppService, node: str, creds=None, wallet=None) -> web.Application:
    app = web.Application()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "node": node})

    async def _plan_template(user_id: int, market: str | None, template: str | None,
                             margin, margin_pct=None) -> dict:
        """Resolve a template + chosen margin into concrete settings + a preview of the
        resulting size/notional/bounds. The WORKER owns the sizing because it reads the
        live balance + price — the UI never computes against a balance. Margin precedence:
        explicit `margin` (USDT) > `margin_pct` (fraction of free balance) > template
        default. Free balance = available margin, falling back to equity (Bybit testnet
        UNIFIED often reports availableBalance as 0)."""
        if template not in prefs.STRATEGY_TEMPLATES:
            raise web.HTTPBadRequest(reason=f"unknown template: {template}")
        if not market:
            raise web.HTTPBadRequest(reason="market required")
        try:
            bal = await service.balance(user_id)
            m = await service.market_info(user_id, market)
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        tpl = prefs.STRATEGY_TEMPLATES[template]
        free = bal.available if bal.available > 0 else bal.equity
        if margin not in (None, ""):
            margin_q = Decimal(str(margin))
        elif margin_pct not in (None, ""):
            margin_q = free * Decimal(str(margin_pct))
        else:
            margin_q = prefs.default_margin(template, free)
        size = prefs.grid_size(template, margin_q, m.mid)
        if size <= 0:
            raise web.HTTPBadRequest(reason="margin/balance too low to size this grid")
        mid = Decimal(m.mid)
        band = Decimal(tpl["band"]) / 100
        return {
            "settings": prefs.template_settings(template, size),
            "market": market, "template": template, "size": str(size), "margin": str(margin_q),
            "notional": str(margin_q * Decimal(tpl["leverage"])), "leverage": tpl["leverage"],
            "levels": tpl["levels"], "currency": bal.currency,
            "free_balance": str(free), "mid": str(mid),
            "lower": str(mid * (1 - band)), "upper": str(mid * (1 + band)),
        }

    async def preview(request: web.Request) -> web.Response:
        body = await request.json()
        plan = await _plan_template(_user_id(request), body.get("market"), body.get("template"),
                                    body.get("margin"), body.get("margin_pct"))
        return web.json_response(plan)

    async def create(request: web.Request) -> web.Response:
        user_id = _user_id(request)
        body = await request.json()
        # One-tap strategy template: resolve it (with the chosen margin) to concrete
        # balance-sized settings here, then fall through to the normal launch path.
        if body.get("template"):
            plan = await _plan_template(user_id, body.get("market"), body["template"],
                                        body.get("margin"), body.get("margin_pct"))
            body["settings"] = {**(body.get("settings") or {}), **plan["settings"]}
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
             "realized_pnl": s.realized_pnl, "fill_count": s.fill_count, "name": s.name}
            for s in rows
        ])

    async def detail(request: web.Request) -> web.Response:
        d = await service.grid_detail(_user_id(request), request.match_info["instance_id"])
        if d is None:
            raise web.HTTPNotFound(reason="no such grid")
        return web.json_response(d)

    async def rename(request: web.Request) -> web.Response:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            raise web.HTTPBadRequest(reason="name required")
        try:
            await service.name_grid(_user_id(request), request.match_info["instance_id"], name[:40])
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"ok": True, "name": name[:40]})

    async def clear_stopped(request: web.Request) -> web.Response:
        return web.json_response({"cleared": await service.clear_stopped(_user_id(request))})

    async def history(request: web.Request) -> web.Response:
        return web.json_response(await service.history(_user_id(request)))

    async def balance(request: web.Request) -> web.Response:
        try:
            b = await service.balance(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"equity": str(b.equity), "available": str(b.available), "currency": b.currency})

    async def positions(request: web.Request) -> web.Response:
        try:
            rows = await service.positions(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response(rows)

    async def orders(request: web.Request) -> web.Response:
        try:
            rows = await service.open_orders(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response(rows)

    async def close_position(request: web.Request) -> web.Response:
        try:
            await service.close_position(_user_id(request), request.match_info["market"])
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"ok": True})

    async def close_all_positions(request: web.Request) -> web.Response:
        try:
            n = await service.close_all_positions(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"closed": n})

    async def cancel_all_orders(request: web.Request) -> web.Response:
        try:
            n = await service.cancel_all_orders(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response({"markets": n})

    async def panic(request: web.Request) -> web.Response:
        try:
            result = await service.panic(_user_id(request))
        except KeyError:
            raise web.HTTPUnauthorized(reason=_NO_CREDS) from None
        except PermissionError as e:
            raise web.HTTPConflict(reason=str(e)) from None
        return web.json_response(result)

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

    async def get_wallet(request: web.Request) -> web.Response:
        if wallet is None:
            raise web.HTTPServiceUnavailable(
                reason="wallet storage not configured — set PERPSAGENT_CRED_MASTER_KEY")
        user_id = _user_id(request)
        address = await wallet.get_or_create(user_id)  # mint on first use
        balance = await wallet.balance(user_id)
        return web.json_response({"address": address, "balance": str(balance),
                                  "currency": "MNT", "faucet": _FAUCET_URL})

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/grids", create)
    app.router.add_post("/v1/grids/preview", preview)
    app.router.add_get("/v1/settings", get_settings)
    app.router.add_put("/v1/settings", put_settings)
    app.router.add_delete("/v1/settings", delete_settings)
    app.router.add_delete("/v1/grids/{instance_id}", stop)
    app.router.add_post("/v1/grids/{instance_id}/pause", pause)
    app.router.add_post("/v1/grids/clear", clear_stopped)
    app.router.add_get("/v1/grids/{instance_id}", detail)
    app.router.add_put("/v1/grids/{instance_id}/name", rename)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/history", history)
    app.router.add_get("/v1/balance", balance)
    app.router.add_get("/v1/positions", positions)
    app.router.add_get("/v1/orders", orders)
    app.router.add_post("/v1/positions/close-all", close_all_positions)
    app.router.add_post("/v1/positions/{market}/close", close_position)
    app.router.add_post("/v1/orders/cancel-all", cancel_all_orders)
    app.router.add_post("/v1/panic", panic)
    app.router.add_get("/v1/market/{market}", market)
    app.router.add_put("/v1/credentials", put_credentials)
    app.router.add_get("/v1/credentials", get_credentials)
    app.router.add_delete("/v1/credentials", delete_credentials)
    app.router.add_get("/v1/wallet", get_wallet)
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
    wallet_admin = None
    fee_payer = None
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
        # Same Fernet codec seals each user's managed MNT wallet (Opsi A) — the
        # builder fee is then debited from the USER's wallet, not the operator.
        from ..adapters.store.wallets import WalletAdmin

        wallet_admin = WalletAdmin(store, codec, rpc_url=s.mantle_rpc or None, chain_id=s.x402_chain_id)
        fee_payer = wallet_admin.pay
    if factory is None:  # dev/demo fallback: in-memory venue, no keys
        from ..adapters.exchanges.fake import FakeExchange

        factory = lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"})  # noqa: E731
    service = AppService(store=store, client_factory=factory, router=router, node=node,
                         chain=_chain_from_settings(s), signals=_signals_from_settings(s),
                         builder_fee=s.builder_fee, fee_asset=s.fee_asset,
                         fee_account=s.fee_account, treasury=s.x402_pay_to, fee_payer=fee_payer)
    return service, creds_admin, wallet_admin


def main() -> None:
    from ..config import Settings
    from .shard import ShardRouter

    s = Settings()
    s.assert_consistent()
    router = ShardRouter(s.shard_count)
    service, creds_admin, wallet_admin = _service_from_settings(s, router, s.shard_node)

    async def _startup(_app):
        recovered = await service.recover()
        print(f"[worker {s.shard_node}] recovered {len(recovered)} instance(s)")

    async def _cleanup(_app):
        await service.drain()  # let fire-then-confirm attest/memory writes land

    app = build_worker_app(service, s.shard_node, creds=creds_admin, wallet=wallet_admin)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    print(f"[worker {s.shard_node}/{s.shard_count}] serving on {s.worker_host}:{s.worker_port}")
    web.run_app(app, host=s.worker_host, port=s.worker_port)


if __name__ == "__main__":
    main()
