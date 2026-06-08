"""Sharded serving: the worker HTTP facade (incl. off-shard rejection) and the
gateway reverse-proxy routing each user to the worker that owns them. All on
FakeExchange + aiohttp test servers (no keys/network)."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from perpsagent.adapters.exchanges.fake import FakeExchange
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
async def test_worker_requires_user_header():
    c = await _client(build_worker_app(_svc(), "0"))
    try:
        assert (await c.post("/v1/grids", json=GRID)).status == 400
    finally:
        await c.close()


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
