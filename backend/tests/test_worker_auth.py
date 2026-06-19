"""The worker trusts X-User-Id, so when it's reachable beyond loopback it must ALSO
require a shared secret (X-Internal-Token). Empty token = open (loopback dev,
unchanged); a set token must be present and correct on every route except /healthz."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.app.worker import build_worker_app


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _svc():
    return AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "equity": "10000"}))


@pytest.mark.asyncio
async def test_no_token_means_open_loopback_dev():
    c = await _client(build_worker_app(_svc(), "0"))  # no internal_token → unchanged
    try:
        assert (await c.get("/v1/balance", headers={"X-User-Id": "7"})).status == 200
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_token_required_when_configured():
    c = await _client(build_worker_app(_svc(), "0", internal_token="s3cret"))
    try:
        assert (await c.get("/healthz")).status == 200                       # liveness probe stays open
        miss = await c.get("/v1/balance", headers={"X-User-Id": "7"})        # forged user, no token
        assert miss.status == 401
        bad = await c.get("/v1/balance", headers={"X-User-Id": "7", "X-Internal-Token": "wrong"})
        assert bad.status == 401
        good = await c.get("/v1/balance", headers={"X-User-Id": "7", "X-Internal-Token": "s3cret"})
        assert good.status == 200
    finally:
        await c.close()
