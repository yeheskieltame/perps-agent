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
from .cache import CachePort, InProcessCache


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
              cache_ttl: float = 5.0, cache: CachePort | None = None) -> web.Application:
    app = web.Application(middlewares=[_payment_middleware(gateway)])
    sigs = signals or []
    # Collapse duplicate work per market: a burst of paid calls would otherwise
    # re-run the signal fan-out + on-chain recall on every request. Response BODIES
    # are cached (JSON), so the same port works in-process or on Redis (shared
    # across UI replicas — pass `cache=RedisCache(...)`).
    cache = cache or InProcessCache(cache_ttl)

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "perpsagent-alpha"})

    async def regime(request: web.Request) -> web.Response:
        market = request.match_info["market"]
        body = await cache.get(f"regime:{market}")
        if body is None:
            fp = await classify_regime(exchange, market, sigs)
            body = {"market": market, "regime": _fp(fp)}
            await cache.put(f"regime:{market}", body)
        return web.json_response(body)

    async def recall(request: web.Request) -> web.Response:
        market = request.match_info["market"]
        body = await cache.get(f"recall:{market}")
        if body is None:
            fp = await classify_regime(exchange, market, sigs)
            records = await recall_best(chain, fp)
            body = {"market": market, "regime": _fp(fp), "episodes": [_record(r) for r in records]}
            await cache.put(f"recall:{market}", body)
        return web.json_response(body)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/alpha/regime/{market}", regime)
    app.router.add_get("/v1/alpha/recall/{market}", recall)
    return app


def _alpha_chain(s):
    """Read side of the verifiable brain. A real MantleChainClient when the Memory
    contract is configured — `recall` then serves VERIFIED on-chain episodes — else
    an in-memory MemoryChain (demo). The alpha API only reads (no signing), so it
    shares the operator key purely for construction; it issues no transactions."""
    if s.mantle_rpc and s.mantle_private_key and s.strategy_memory_addr:
        from ..adapters.chain.client import MantleChainClient

        return MantleChainClient(
            rpc_url=s.mantle_rpc, private_key=s.mantle_private_key,
            ledger_addr=s.strategy_ledger_addr or ("0x" + "00" * 20),
            memory_addr=s.strategy_memory_addr, vault_addr=s.vault_addr or None,
            detail_path=s.memory_detail_path or None,
        )
    from ..adapters.chain.memory_chain import MemoryChain

    return MemoryChain()


def _alpha_exchange(s):
    """Microstructure source for the regime fingerprint. Operator Bybit keys when
    set, else a FakeExchange so the server always starts (recall stays real even
    when regime is demo-quality)."""
    if s.bybit_api_key and s.bybit_api_secret:
        from ..adapters.exchanges.bybit.adapter import BybitExchange

        return BybitExchange({"api_key": s.bybit_api_key, "api_secret": s.bybit_api_secret,
                              "testnet": s.bybit_testnet, "rate_limit": s.bybit_rate_limit,
                              "max_retries": s.bybit_max_retries})
    from ..adapters.exchanges.fake import FakeExchange

    return FakeExchange({"mid": "100"})


def _alpha_signals(s) -> list:
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


def main() -> None:
    """Serve the x402-gated alpha API. Uses the real chain/exchange/signals when
    configured (production), else fakes so the 402 flow is demoable without keys."""
    from ..config import Settings

    s = Settings()
    gateway = X402Gateway(
        pay_to=s.x402_pay_to or "0x" + "00" * 20,
        # Native MNT: pay the gas token directly, verified on-chain via the Mantle RPC.
        # ERC-20 mode (x402_native=false): an EIP-3009 token address via a facilitator.
        asset=("MNT" if s.x402_native else (s.x402_asset or "0x" + "00" * 20)),
        network=s.x402_network,
        chain_id=s.x402_chain_id,
        default_price=s.x402_price,
        facilitator_url=s.x402_facilitator_url or None,
        native=s.x402_native,
        rpc_url=s.mantle_rpc or None,
    )
    cache: CachePort | None = None
    if s.redis_url:  # shared cache across replicas
        from ..adapters.cache.redis_cache import RedisCache

        cache = RedisCache(s.redis_url, s.alpha_cache_ttl_s)
    chain = _alpha_chain(s)
    live = type(chain).__name__ == "MantleChainClient"
    settle = "native-mnt" if s.x402_native else ("facilitator" if s.x402_facilitator_url else "local-verify")
    print(f"[alpha] serving on :{s.alpha_port}  recall={'on-chain' if live else 'in-memory'} · settle={settle}")
    web.run_app(build_app(gateway, _alpha_exchange(s), chain, signals=_alpha_signals(s),
                          cache_ttl=s.alpha_cache_ttl_s, cache=cache), port=s.alpha_port)


if __name__ == "__main__":
    main()
