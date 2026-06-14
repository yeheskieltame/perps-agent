"""Command logic against a fake API — every user-facing path, offline."""
import pytest

from perpsbot.api import ApiError
from perpsbot import commands


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail: ApiError | None = None
        self.rows = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    async def create_grid(self, uid, market, band, levels, size, leverage="1"):
        self.calls.append(("create", uid, market, band, levels, size))
        self._maybe_fail()
        return f"{market}-0-abc123"

    async def status(self, uid):
        self._maybe_fail()
        return self.rows

    async def stop(self, uid, iid):
        self.calls.append(("stop", uid, iid))
        self._maybe_fail()

    async def pause(self, uid, iid):
        self.calls.append(("pause", uid, iid))
        self._maybe_fail()

    async def market(self, uid, market):
        self._maybe_fail()
        return {"market": market, "bid": "99.9", "ask": "100.1", "mid": "100.0"}

    async def balance(self, uid):
        self._maybe_fail()
        return {"equity": "73193.62", "available": "73000.00", "currency": "USDT"}


async def test_grid_defaults():
    api = FakeAPI()
    out = await commands.grid(api, 42, "btcusdt")
    assert api.calls == [("create", 42, "BTCUSDT", "0.01", 10, "0.001")]
    assert "Grid launched" in out and "BTCUSDT-0-abc123" in out and "±1%" in out


async def test_grid_full_args_percent_to_fraction():
    api = FakeAPI()
    out = await commands.grid(api, 42, "ETHUSDT 0.8 12 0.002")
    assert api.calls == [("create", 42, "ETHUSDT", "0.008", 12, "0.002")]
    assert "±0.8%" in out and "12 levels" in out


async def test_grid_rejects_bad_args_without_calling_api():
    api = FakeAPI()
    assert "Usage" in await commands.grid(api, 42, "")
    assert "Band" in await commands.grid(api, 42, "BTCUSDT 15")          # >10%
    assert "Levels" in await commands.grid(api, 42, "BTCUSDT 1 999")
    assert "Size" in await commands.grid(api, 42, "BTCUSDT 1 10 -2")
    assert "parse" in await commands.grid(api, 42, "BTCUSDT one")
    assert api.calls == []   # nothing reached the backend


async def test_grid_surfaces_backend_refusal():
    api = FakeAPI()
    api.fail = ApiError(409, "user 42 is not on shard 0")
    out = await commands.grid(api, 42, "BTCUSDT")
    assert "409" in out and "shard" in out


async def test_status_empty_and_rows():
    api = FakeAPI()
    assert "No grids yet" in await commands.status(api, 42)
    api.rows = [{"instance_id": "i1", "state": "RUNNING", "realized_pnl": "0.236", "fill_count": 12}]
    out = await commands.status(api, 42)
    assert "i1" in out and "RUNNING" in out and "0.236" in out and "12" in out


async def test_stop_and_pause():
    api = FakeAPI()
    assert "Usage" in await commands.stop(api, 42, "")
    assert "Stopped" in await commands.stop(api, 42, "i1")
    assert "Paused" in await commands.pause(api, 42, "i1")
    assert ("stop", 42, "i1") in api.calls and ("pause", 42, "i1") in api.calls


async def test_price_and_balance():
    api = FakeAPI()
    assert "mid 100.0" in await commands.price(api, 42, "btcusdt")
    assert "Usage" in await commands.price(api, 42, "")
    assert "73193.62 USDT" in await commands.balance(api, 42)
