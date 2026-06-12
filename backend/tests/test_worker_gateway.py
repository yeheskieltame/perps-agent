"""Sharded serving: the worker HTTP facade (incl. off-shard rejection) and the
gateway reverse-proxy routing each user to the worker that owns them. All on
FakeExchange + aiohttp test servers (no keys/network)."""
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.credentials import (
    CredentialAdmin, CredentialCodec, credential_client_factory,
)
from perpsagent.app.gateway import build_gateway_app
from perpsagent.app.service import AppService
from perpsagent.app.shard import ShardRouter
from perpsagent.app.worker import build_worker_app

GRID = {"market": "BTCUSDT", "lower": "90", "upper": "110", "levels": 11,
        "order_size": "0.01", "venue": "fake", "spacing": "arithmetic"}


def _svc(router=None, node=None) -> AppService:
    return AppService(client_factory=lambda uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                      router=router, node=node)


async def _client(app) -> TestClient:
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


@pytest.mark.asyncio
async def test_worker_crud_flow():
    c = await _client(build_worker_app(_svc(), "0"))
    try:
        assert (await c.get("/healthz")).status == 200
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"}, json=GRID)
        assert r.status == 200
        iid = (await r.json())["instance_id"]
        st = await (await c.get("/v1/status", headers={"X-User-Id": "7"})).json()
        assert [s["instance_id"] for s in st] == [iid]
        bal = await (await c.get("/v1/balance", headers={"X-User-Id": "7"})).json()
        assert "equity" in bal
        assert (await c.delete(f"/v1/grids/{iid}", headers={"X-User-Id": "7"})).status == 200
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_rejects_off_shard_user():
    router = ShardRouter(["0", "1"])
    foreign = next(u for u in range(1000) if router.owns(u, "1"))   # belongs to shard 1
    c = await _client(build_worker_app(_svc(router, "0"), "0"))     # this worker is shard 0
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": str(foreign)}, json=GRID)
        assert r.status == 409   # wrong shard
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_market_info():
    c = await _client(build_worker_app(_svc(), "0"))
    try:
        m = await (await c.get("/v1/market/BTCUSDT", headers={"X-User-Id": "7"})).json()
        assert m == {"market": "BTCUSDT", "bid": "99.9", "ask": "100.1", "mid": "100.0"}
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_create_from_band_shorthand():
    """A UI may send {band} instead of lower/upper — the worker resolves bounds
    from the user's live top-of-book (mid 100 ± 1% -> [99, 101])."""
    c = await _client(build_worker_app(_svc(), "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "band": "0.01", "levels": 11,
                               "order_size": "0.01", "venue": "fake"})
        assert r.status == 200
        st = await (await c.get("/v1/status", headers={"X-User-Id": "7"})).json()
        assert len(st) == 1 and st[0]["state"] == "RUNNING"

        bad = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                           json={"market": "BTCUSDT", "band": "1.5", "levels": 11})
        assert bad.status == 400   # band must be a fraction in (0, 1)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_requires_user_header():
    c = await _client(build_worker_app(_svc(), "0"))
    try:
        assert (await c.post("/v1/grids", json=GRID)).status == 400
    finally:
        await c.close()


class MemCredsStore:
    """In-memory CredentialStorePort for tests."""

    def __init__(self):
        self.rows: dict[int, bytes] = {}

    async def put_credentials(self, user_id, ciphertext):
        self.rows[user_id] = ciphertext

    async def get_credentials(self, user_id):
        return self.rows.get(user_id)

    async def delete_credentials(self, user_id):
        self.rows.pop(user_id, None)


def _creds_app():
    codec = CredentialCodec(CredentialCodec.generate_key())
    store = MemCredsStore()
    factory = credential_client_factory(
        store, codec, lambda creds: FakeExchange({"mid": "100", "tick": "0.1"}))
    service = AppService(client_factory=factory)
    return build_worker_app(service, "0", creds=CredentialAdmin(store, codec))


@pytest.mark.asyncio
async def test_worker_credentials_flow():
    """connect → trade → disconnect: 401 without keys, 200 with, 401 again after."""
    c = await _client(_creds_app())
    h = {"X-User-Id": "42"}
    try:
        assert await (await c.get("/v1/credentials", headers=h)).json() == {"connected": False}
        assert (await c.post("/v1/grids", headers=h, json=GRID)).status == 401

        r = await c.put("/v1/credentials", headers=h,
                        json={"api_key": "testkey123", "api_secret": "s3cr3t-value"})
        assert r.status == 200
        assert (await r.json()) == {"ok": True, "testnet": True}   # testnet-first default

        info = await (await c.get("/v1/credentials", headers=h)).json()
        assert info["connected"] is True and info["testnet"] is True
        assert "s3cr3t-value" not in str(info)        # API never echoes a secret
        assert info["key_preview"] == "test…"

        assert (await c.post("/v1/grids", headers=h, json=GRID)).status == 200
        assert (await c.get("/v1/balance", headers=h)).status == 200

        assert (await c.delete("/v1/credentials", headers=h)).status == 200
        assert await (await c.get("/v1/credentials", headers=h)).json() == {"connected": False}
        assert (await c.get("/v1/balance", headers=h)).status == 401   # session torn down
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_credentials_503_when_not_configured():
    c = await _client(build_worker_app(_svc(), "0"))   # no creds admin wired
    try:
        r = await c.put("/v1/credentials", headers={"X-User-Id": "7"},
                        json={"api_key": "k", "api_secret": "s"})
        assert r.status == 503
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_sqlite_store_credentials_roundtrip(tmp_path):
    from perpsagent.adapters.store.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "t.db"))
    assert await store.get_credentials(1) is None
    await store.put_credentials(1, b"blob-a")
    await store.put_credentials(1, b"blob-b")          # upsert
    assert await store.get_credentials(1) == b"blob-b"
    await store.delete_credentials(1)
    assert await store.get_credentials(1) is None
    await store.close()


@pytest.mark.asyncio
async def test_gateway_routes_to_owning_worker():
    router = ShardRouter(2)  # nodes "0","1"
    w0 = await _client(build_worker_app(_svc(router, "0"), "0"))
    w1 = await _client(build_worker_app(_svc(router, "1"), "1"))
    urls = {"0": str(w0.make_url("/")).rstrip("/"), "1": str(w1.make_url("/")).rstrip("/")}
    gw = await _client(build_gateway_app(ShardRouter(2), urls))
    try:
        u = next(u for u in range(1000) if router.owns(u, "1"))     # a shard-1 user
        r = await gw.post("/v1/grids", headers={"X-User-Id": str(u)}, json=GRID)
        assert r.status == 200
        iid = (await r.json())["instance_id"]

        # via the gateway, status for u routes to w1 and shows the grid
        via_gw = await (await gw.get("/v1/status", headers={"X-User-Id": str(u)})).json()
        assert [s["instance_id"] for s in via_gw] == [iid]
        # shard 0 never saw this user
        assert await (await w0.get("/v1/status", headers={"X-User-Id": str(u)})).json() == []
    finally:
        await gw.close()
        await w0.close()
        await w1.close()


@pytest.mark.asyncio
async def test_settings_crud_and_knobs_reach_the_engine():
    """The Telegram path: save settings → /grid with no args uses them, and the
    engine actually receives the guards/timeframe they describe."""
    svc = _svc()
    c = await _client(build_worker_app(svc, "0"))
    h = {"X-User-Id": "7"}
    try:
        # defaults out of the box, nothing customized
        s0 = await (await c.get("/v1/settings", headers=h)).json()
        assert s0["settings"]["leverage"] == "1" and s0["customized"] == []

        # save a few knobs (with aliases); unknown/bad ones are 400 + named
        r = await c.put("/v1/settings", headers=h,
                        json={"lev": "25", "band": "2", "tp": "0.5", "tf": "1h"})
        assert r.status == 200
        s1 = (await r.json())["settings"]
        assert (s1["leverage"], s1["band"], s1["tp"], s1["timeframe"]) == ("25", "2", "0.5", "1h")
        assert (await c.put("/v1/settings", headers=h, json={"banana": "1"})).status == 400
        assert (await c.put("/v1/settings", headers=h, json={"leverage": "0"})).status == 400

        # /grid with just a market: bounds from the saved band (mid 100 ± 2%)
        r = await c.post("/v1/grids", headers=h, json={"market": "BTCUSDT"})
        assert r.status == 200
        body = await r.json()
        assert body["effective"]["leverage"] == "25"
        assert (Decimal(body["lower"]), Decimal(body["upper"])) == (Decimal("98"), Decimal("102"))

        # the engine got the knobs, not just the response text
        eng = svc._sessions[7].engines()[0]
        assert eng.cfg.leverage == 25
        assert eng.profit_guard.take_profit == Decimal("0.5")
        assert eng.breaker.max_inventory > 0          # auto cap is on by default
        assert eng.timeframe == "60" and eng.monitor_interval == 900.0  # 1h bar/4

        # per-launch override beats saved; reset restores defaults
        r = await c.post("/v1/grids", headers=h,
                         json={"market": "BTCUSDT", "settings": {"leverage": "5", "bias": "long"}})
        assert (await r.json())["effective"]["leverage"] == "5"
        assert svc._sessions[7].engines()[-1].cfg.bias == 1
        assert (await c.delete("/v1/settings", headers=h)).status == 200
        s2 = await (await c.get("/v1/settings", headers=h)).json()
        assert s2["settings"]["leverage"] == "1"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_legacy_grid_body_still_wins_over_knobs():
    """Pre-settings callers send levels/order_size/leverage top-level and band as
    a FRACTION — that contract must keep working unchanged."""
    svc = _svc()
    c = await _client(build_worker_app(svc, "0"))
    h = {"X-User-Id": "9"}
    try:
        r = await c.post("/v1/grids", headers=h, json=GRID)   # explicit lower/upper
        assert r.status == 200
        eng = svc._sessions[9].engines()[0]
        assert eng.cfg.levels == 11 and eng.cfg.order_size == Decimal("0.01")
        r = await c.post("/v1/grids", headers=h,
                         json={"market": "BTCUSDT", "band": "0.01", "levels": 5})
        assert r.status == 200
        body = await r.json()
        assert (Decimal(body["lower"]), Decimal(body["upper"])) == (Decimal("99"), Decimal("101"))  # fraction, not %
    finally:
        await c.close()
