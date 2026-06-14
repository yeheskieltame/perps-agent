"""One-tap strategy templates: the auto-size math, and the worker resolving a
template into balance-sized settings at launch. FakeExchange (no keys/network)."""
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app import prefs
from perpsagent.app.service import AppService
from perpsagent.app.worker import build_worker_app
from perpsagent.domain.models import BalanceView


class _ZeroAvailExchange(FakeExchange):
    """Bybit testnet UNIFIED quirk: availableBalance reported as 0 with healthy equity."""

    async def balance(self) -> BalanceView:
        return BalanceView(equity=self._equity, available=Decimal("0"))


def test_autosize_scales_with_balance_leverage_and_levels():
    # aggressive: margin .80 × lev 25 over 8 levels: 80000*.8*25 / (8*100) = 2000
    assert prefs.autosize("aggressive", 80000, 100) == Decimal("2000")
    # safe: margin .15 × lev 1 over 10 levels: 80000*.15 / (10*100) = 12
    assert prefs.autosize("safe", 80000, 100) == Decimal("12")
    assert prefs.autosize("balanced", 0, 100) == Decimal("0")    # no balance → 0
    assert prefs.autosize("safe", 80000, 0) == Decimal("0")      # no price → 0


def test_template_settings_shape():
    assert prefs.template_settings("aggressive", Decimal("2000")) == \
        {"band": "2", "levels": "8", "leverage": "25", "size": "2000"}


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


@pytest.mark.asyncio
async def test_worker_launches_a_template_autosized_from_balance():
    svc = AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "equity": "10000"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "safe"})
        assert r.status == 200
        body = await r.json()
        eff = body["effective"]
        assert eff["leverage"] == "1" and eff["levels"] == "10"
        assert Decimal(eff["size"]) == Decimal("1.5")               # 10000*.15 / (10*100)
        assert Decimal(body["lower"]) == Decimal("99.5")            # ±0.5% around mid 100
        assert Decimal(body["upper"]) == Decimal("100.5")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_preview_sizes_from_the_chosen_margin():
    svc = AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "equity": "10000"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids/preview", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "aggressive", "margin": "1000"})
        assert r.status == 200
        p = await r.json()
        assert Decimal(p["notional"]) == Decimal("25000")          # margin 1000 × lev 25
        assert Decimal(p["size"]) == Decimal("31.25")              # 25000 / (8 × 100)
        assert p["leverage"] == "25" and p["levels"] == 8
        assert Decimal(p["lower"]) == Decimal("98") and Decimal(p["upper"]) == Decimal("102")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_sizes_from_margin_pct_of_balance():
    # 25% of equity 10000 = 2500 margin; balanced lev 5 → notional 12500; /(10×100) = 12.5 size.
    ex = FakeExchange({"mid": "100", "equity": "10000"})
    svc = AppService(client_factory=lambda _u: ex)
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids/preview", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "balanced", "margin_pct": "0.25"})
        assert r.status == 200
        p = await r.json()
        assert Decimal(p["margin"]) == Decimal("2500")            # 25% of the balance
        assert Decimal(p["notional"]) == Decimal("12500")
        assert Decimal(p["size"]) == Decimal("12.5")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_margin_pct_falls_back_to_equity_when_available_is_zero():
    svc = AppService(client_factory=lambda _u: _ZeroAvailExchange({"mid": "100", "equity": "10000"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids/preview", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "safe", "margin_pct": "0.25"})
        assert r.status == 200
        p = await r.json()
        assert Decimal(p["margin"]) == Decimal("2500")           # 25% of EQUITY (available was 0)
        assert Decimal(p["size"]) > 0                            # not "too low"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_rejects_unknown_template():
    svc = AppService(client_factory=lambda _u: FakeExchange({"mid": "100", "equity": "10000"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "nope"})
        assert r.status == 400
    finally:
        await c.close()
