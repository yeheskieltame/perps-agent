"""Gateway — the stateless front door that routes each request to the worker owning
the user (docs.perpsagent.xyz).

It reads `X-User-Id`, computes `shard = ShardRouter.route(user_id)`, and reverse-
proxies the call to that shard's worker URL. Because routing is consistent hashing
over hashlib, every gateway replica routes identically — run several behind a load
balancer. The workers hold all the state; the gateway holds none.

Scope: the single-host deploy (deploy/) talks to one worker directly; this gateway
is the horizontal-scale path for several workers. It is unauthenticated by design —
terminate auth at your ingress before exposing it (retries/SSE pass-through live
here too when needed).

  PERPSAGENT_SHARD_COUNT=2 PERPSAGENT_GATEWAY_PORT=8080 \
  PERPSAGENT_SHARD_URLS='{"0":"http://127.0.0.1:9000","1":"http://127.0.0.1:9001"}' \
    python -m perpsagent.app.gateway
"""
from __future__ import annotations

import aiohttp
from aiohttp import web

_SESSION: web.AppKey = web.AppKey("session", aiohttp.ClientSession)


def build_gateway_app(router, shard_urls: dict[str, str]) -> web.Application:
    app = web.Application()

    async def _open_session(app: web.Application) -> None:
        app[_SESSION] = aiohttp.ClientSession()

    async def _close_session(app: web.Application) -> None:
        await app[_SESSION].close()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "shards": sorted(shard_urls)})

    async def proxy(request: web.Request) -> web.Response:
        raw = request.headers.get("X-User-Id")
        if raw is None:
            raise web.HTTPBadRequest(reason="missing X-User-Id header")
        try:
            node = router.route(int(raw))
        except ValueError:
            raise web.HTTPBadRequest(reason="X-User-Id must be an integer") from None
        base = shard_urls.get(node)
        if base is None:
            raise web.HTTPBadGateway(reason=f"no worker URL for shard {node}")

        url = base.rstrip("/") + request.rel_url.path
        body = await request.read()
        session = request.app[_SESSION]
        async with session.request(
            request.method, url, params=request.rel_url.query, data=body,
            headers={"X-User-Id": raw, "Content-Type": request.content_type},
        ) as resp:
            return web.Response(status=resp.status, body=await resp.read(),
                                content_type=resp.content_type)

    app.on_startup.append(_open_session)
    app.on_cleanup.append(_close_session)
    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/v1/{tail:.*}", proxy)  # everything else → owning worker
    return app


def main() -> None:
    import json

    from ..config import Settings
    from .shard import ShardRouter

    s = Settings()
    router = ShardRouter(s.shard_count)
    shard_urls = json.loads(s.shard_urls) if s.shard_urls else {}
    if not shard_urls:
        raise SystemExit("set PERPSAGENT_SHARD_URLS, e.g. '{\"0\":\"http://127.0.0.1:9000\"}'")
    print(f"[gateway] routing {s.shard_count} shard(s) on :{s.gateway_port} -> {sorted(shard_urls)}")
    web.run_app(build_gateway_app(router, shard_urls), port=s.gateway_port)


if __name__ == "__main__":
    main()
