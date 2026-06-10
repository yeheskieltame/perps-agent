"""WorkerAPI against an aiohttp test server mimicking the worker — verifies the
X-User-Id header, payload shapes, and error surfacing. Offline."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from perpsbot.api import ApiError, WorkerAPI


def _fake_worker(seen: list) -> web.Application:
    app = web.Application()

    async def create(request: web.Request) -> web.Response:
        seen.append((request.headers.get("X-User-Id"), await request.json()))
        return web.json_response({"instance_id": "BTCUSDT-0-abc123"})

    async def status(request: web.Request) -> web.Response:
        if request.headers.get("X-User-Id") != "42":
            raise web.HTTPConflict(reason="not your grid instance")
        return web.json_response([{"instance_id": "i1", "state": "RUNNING",
                                   "realized_pnl": "0.1", "fill_count": 3}])

    app.router.add_post("/v1/grids", create)
    app.router.add_get("/v1/status", status)
    return app


async def test_create_sends_band_body_and_user_header():
    seen: list = []
    server = TestServer(_fake_worker(seen))
    await server.start_server()
    api = WorkerAPI(str(server.make_url("/")))
    try:
        iid = await api.create_grid(42, "BTCUSDT", "0.01", 10, "0.001")
        assert iid == "BTCUSDT-0-abc123"
        uid, body = seen[0]
        assert uid == "42"
        assert body == {"market": "BTCUSDT", "band": "0.01", "levels": 10,
                        "order_size": "0.001", "leverage": "1"}
    finally:
        await api.close()
        await server.close()


async def test_non_200_raises_api_error_with_detail():
    server = TestServer(_fake_worker([]))
    await server.start_server()
    api = WorkerAPI(str(server.make_url("/")))
    try:
        rows = await api.status(42)                      # owned -> 200
        assert rows[0]["instance_id"] == "i1"
        with pytest.raises(ApiError) as ei:
            await api.status(7)                          # foreign -> 409
        assert ei.value.status == 409
        assert "not your grid" in ei.value.detail
    finally:
        await api.close()
        await server.close()
