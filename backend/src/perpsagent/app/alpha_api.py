"""Perps Agent alpha API — x402-gated endpoints selling verified intelligence
(the "two-sided asset": our agent's brain, sold per call — docs/CONCEPT.md §7).

    GET /healthz                      free
    GET /v1/alpha/regime/{market}     paid — current fused regime fingerprint
    GET /v1/alpha/recall/{market}     paid — best VERIFIED episodes for this regime

Unpaid → HTTP 402 + PaymentRequirements; pay with an EIP-3009 USDC authorization
in `X-PAYMENT`; response carries `X-PAYMENT-RESPONSE` with the settlement result.

Dev server (fakes, no keys):  PYTHONPATH=src python3 -m perpsagent.app.alpha_api
"""
from __future__ import annotations

from aiohttp import web

from ..adapters.payments.x402 import X402Gateway, b64decode_json, b64encode_json
from ..agent.recall import recall_best
from ..agent.sense import classify_regime
from .cache import TTLCache


def _payment_middleware(gateway: X402Gateway):
    @web.middleware
    async def mw(request: web.Request, handler):
        if not request.path.startswith("/v1/alpha/"):
            return await handler(request)
        resource = str(request.url)
        header = request.headers.get("X-PAYMENT")
        if not header:
            return web.json_response(gateway.challenge(resource), status=402)
        try:
            payment = b64decode_json(header)
        except Exception:
            return web.json_response(gateway.challenge(resource, error="malformed X-PAYMENT header"), status=402)
        req = gateway.requirements(resource)
        ok, reason = await gateway.verify(payment, req)
        if not ok:
            return web.json_response(gateway.challenge(resource, error=reason), status=402)
        response = await handler(request)
        response.headers["X-PAYMENT-RESPONSE"] = b64encode_json(await gateway.settle(payment, req))
        return response

    return mw


def _fp(fp) -> dict:
    return dict(fp.__dict__)


def _record(r) -> dict:
    return {
        "config": {
            "market": r.config.market, "lower": str(r.config.lower), "upper": str(r.config.upper),
            "levels": r.config.levels, "spacing": r.config.spacing.value, "order_size": str(r.config.order_size),
        },
        "outcome": {
            "realized_pnl": str(r.outcome.realized_pnl), "winrate": r.outcome.winrate,
            "risk_adjusted": r.outcome.risk_adjusted, "is_backtest": r.outcome.is_backtest,
        },
        "regime": _fp(r.regime),
    }


def build_app(gateway: X402Gateway, exchange, chain, signals: list | None = None,
              cache_ttl: float = 5.0) -> web.Application:
    app = web.Application(middlewares=[_payment_middleware(gateway)])
    sigs = signals or []
    # Collapse duplicate work per market: a burst of paid calls would otherwise
    # re-run the signal fan-out + on-chain recall on every request.
    regime_cache: TTLCache = TTLCache(cache_ttl)
    recall_cache: TTLCache = TTLCache(cache_ttl)

    async def _regime(market: str):
        fp = regime_cache.get(market)
        if fp is None:
            fp = await classify_regime(exchange, market, sigs)
            regime_cache.put(market, fp)
        return fp

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "perpsagent-alpha"})

    async def regime(request: web.Request) -> web.Response:
        market = request.match_info["market"]
        fp = await _regime(market)
        return web.json_response({"market": market, "regime": _fp(fp)})

    async def recall(request: web.Request) -> web.Response:
        market = request.match_info["market"]
        cached = recall_cache.get(market)
        if cached is not None:
            return web.json_response(cached)
        fp = await _regime(market)
        records = await recall_best(chain, fp)
        body = {"market": market, "regime": _fp(fp), "episodes": [_record(r) for r in records]}
        recall_cache.put(market, body)
        return web.json_response(body)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/alpha/regime/{market}", regime)
    app.router.add_get("/v1/alpha/recall/{market}", recall)
    return app


def main() -> None:
    """Dev mode: serves on fakes so the 402 flow is demoable without any keys."""
    from ..adapters.chain.memory_chain import MemoryChain
    from ..adapters.exchanges.fake import FakeExchange
    from ..config import Settings

    s = Settings()
    gateway = X402Gateway(
        pay_to=s.x402_pay_to or "0x" + "00" * 20,
        asset=s.x402_asset or "0x" + "00" * 20,
        network=s.x402_network,
        chain_id=s.x402_chain_id,
        default_price=s.x402_price,
        facilitator_url=s.x402_facilitator_url or None,
    )
    web.run_app(build_app(gateway, FakeExchange({"mid": "100"}), MemoryChain(),
                          cache_ttl=s.alpha_cache_ttl_s), port=s.alpha_port)


if __name__ == "__main__":
    main()
