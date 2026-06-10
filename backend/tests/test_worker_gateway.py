"""Sharded serving: the worker HTTP facade (incl. off-shard rejection) and the
gateway reverse-proxy routing each user to the worker that owns them. All on
FakeExchange + aiohttp test servers (no keys/network)."""
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
