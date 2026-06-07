"""End-to-end x402 flow over real HTTP: 402 challenge → signed USDC authorization
→ 200 with verified alpha + X-PAYMENT-RESPONSE settlement header."""
import os
import time
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.payments.x402 import X402Gateway, b64decode_json, b64encode_json
from perpsagent.agent.sense import classify_regime
from perpsagent.app.alpha_api import build_app
from perpsagent.domain.models import EpisodeOutcome, GridConfig, MemoryRecord, Spacing, Venue

PAY_TO = "0x" + "aa" * 20
ASSET = "0x" + "bb" * 20


@pytest.mark.asyncio
async def test_alpha_api_402_then_paid_200():
    g = X402Gateway(pay_to=PAY_TO, asset=ASSET)
    ex = FakeExchange({"mid": "100"})
    chain = MemoryChain()

    # seed one verified episode in the CURRENT regime so recall returns alpha
    fp = await classify_regime(ex, "BTCUSDT", [])
    cfg = GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                     Decimal("0.01"), Spacing.GEOMETRIC)
    out = EpisodeOutcome("i1", Decimal("1.5"), 0.7, 0.0, 10, "0x" + "ab" * 32, 0.9, False)
    await chain.write_memory(MemoryRecord(regime=fp, config=cfg, outcome=out))

    client = TestClient(TestServer(build_app(g, ex, chain)))
    await client.start_server()
    try:
        assert (await client.get("/healthz")).status == 200          # free endpoint

        r1 = await client.get("/v1/alpha/recall/BTCUSDT")            # unpaid → 402
        assert r1.status == 402
        ch = await r1.json()
        assert ch["x402Version"] == 1 and ch["accepts"][0]["payTo"] == PAY_TO

        acct = Account.create()                                       # pay → 200
        auth = {
            "from": acct.address, "to": PAY_TO,
            "value": ch["accepts"][0]["maxAmountRequired"], "validAfter": "0",
            "validBefore": str(int(time.time()) + 600), "nonce": "0x" + os.urandom(32).hex(),
        }
        signed = Account.sign_typed_data(acct.key, full_message=g._typed_data(auth))
        sig = signed.signature.hex()
        payment = {"x402Version": 1, "scheme": "exact", "network": g.network,
                   "payload": {"signature": sig if sig.startswith("0x") else "0x" + sig,
                                "authorization": auth}}
        r2 = await client.get("/v1/alpha/recall/BTCUSDT",
                              headers={"X-PAYMENT": b64encode_json(payment)})
        assert r2.status == 200
        body = await r2.json()
        assert body["episodes"] and body["episodes"][0]["outcome"]["winrate"] == 0.7

        receipt = b64decode_json(r2.headers["X-PAYMENT-RESPONSE"])
        assert receipt["success"] and receipt["mode"] == "deferred"
        assert receipt["payer"].lower() == acct.address.lower()
    finally:
        await client.close()


class _CountingFake(FakeExchange):
    """Counts best_bid_ask calls — one per uncached classify_regime."""

    def __init__(self, config=None):
        super().__init__(config)
        self.bba_calls = 0

    async def best_bid_ask(self, market):
        self.bba_calls += 1
        return await super().best_bid_ask(market)


def _sign_payment(g, ch):
    acct = Account.create()
    auth = {
        "from": acct.address, "to": ch["accepts"][0]["payTo"],
        "value": ch["accepts"][0]["maxAmountRequired"], "validAfter": "0",
        "validBefore": str(int(time.time()) + 600), "nonce": "0x" + os.urandom(32).hex(),
    }
    sig = Account.sign_typed_data(acct.key, full_message=g._typed_data(auth)).signature.hex()
    return {"x402Version": 1, "scheme": "exact", "network": g.network,
            "payload": {"signature": sig if sig.startswith("0x") else "0x" + sig, "authorization": auth}}


@pytest.mark.asyncio
async def test_alpha_api_caches_regime_within_ttl():
    g = X402Gateway(pay_to=PAY_TO, asset=ASSET)
    ex = _CountingFake({"mid": "100"})
    client = TestClient(TestServer(build_app(g, ex, MemoryChain(), cache_ttl=60.0)))
    await client.start_server()
    try:
        ch = await (await client.get("/v1/alpha/regime/BTCUSDT")).json()   # 402 → challenge
        # two DISTINCT payments (fresh nonces) — the gateway rejects nonce replay
        h1 = {"X-PAYMENT": b64encode_json(_sign_payment(g, ch))}
        h2 = {"X-PAYMENT": b64encode_json(_sign_payment(g, ch))}
        assert (await client.get("/v1/alpha/regime/BTCUSDT", headers=h1)).status == 200
        assert (await client.get("/v1/alpha/regime/BTCUSDT", headers=h2)).status == 200
        assert ex.bba_calls == 1   # second paid call served from the TTL cache
    finally:
        await client.close()
