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


class _LowAvailExchange(FakeExchange):
    """Open positions: free margin (available) is below total equity. The % must be
    taken from `available`, never `equity`."""

    async def balance(self) -> BalanceView:
        return BalanceView(equity=self._equity, available=Decimal("8000"))


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
async def test_margin_pct_is_of_available_margin_not_equity():
    # Positions open: available 8000 < equity 10000. 25% must size off the 8000 of
    # free USDT margin (= 2000), NOT total equity (which would wrongly give 2500).
    svc = AppService(client_factory=lambda _u: _LowAvailExchange({"mid": "100", "equity": "10000"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids/preview", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "balanced", "margin_pct": "0.25"})
        assert r.status == 200
        p = await r.json()
        assert Decimal(p["margin"]) == Decimal("2000")           # 25% of AVAILABLE, not equity
        assert Decimal(p["free_balance"]) == Decimal("8000")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_center_mean_leans_toward_recent_average_not_the_live_top():
    # Live price 100 but the last 200 bars averaged 98 → price sits at the range TOP.
    # 'mean' center clamps to ±band of mid, so the grid shifts DOWN (into the range)
    # instead of centering on the top. band 1% → center clamps to 99 → [98.01, 99.99].
    ex = FakeExchange({"mid": "100", "tick": "0.01", "closes": ["98"] * 200, "equity": "100000"})
    svc = AppService(client_factory=lambda _u: ex)
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "settings": {"band": "1", "levels": "4",
                               "size": "0.001", "anchor": "mean"}})
        assert r.status == 200
        b = await r.json()
        assert Decimal(b["upper"]) < Decimal("100")          # grid sits below the live top
        assert Decimal(b["lower"]) == Decimal("98.01") and Decimal(b["upper"]) == Decimal("99.99")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_center_now_stays_symmetric_on_the_live_price():
    ex = FakeExchange({"mid": "100", "tick": "0.01", "closes": ["98"] * 200, "equity": "100000"})
    svc = AppService(client_factory=lambda _u: ex)
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "settings": {"band": "1", "levels": "4",
                               "size": "0.001", "anchor": "now"}})
        b = await r.json()
        assert Decimal(b["lower"]) == Decimal("99") and Decimal(b["upper"]) == Decimal("101")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_auto_bias_goes_long_with_a_strong_uptrend_and_caps_loss():
    # The agent's brain on the PRODUCT path: a steep uptrend → grid biases LONG
    # (buys dips / banks the rises) instead of shorting into the rally. The worker
    # also auto-fills a per-grid loss cap (the backstop that was missing).
    up = [str(100 + i * 0.4) for i in range(200)]
    ex = FakeExchange({"mid": up[-1], "equity": "100000", "closes": up})
    svc = AppService(client_factory=lambda _u: ex)
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "settings": {"band": "1", "levels": "10",
                               "size": "0.01", "bias": "auto"}})
        assert r.status == 200
        b = await r.json()
        assert b["decision"]["mode"] == "auto" and b["decision"]["bias"] == 1
        assert Decimal(b["effective"]["max_drawdown"]) > 0          # auto loss-cap backstop
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_auto_neutral_when_ranging_and_pinned_bias_respected():
    flat = ["100"] * 200
    ex = FakeExchange({"mid": "100", "equity": "100000", "closes": flat})
    svc = AppService(client_factory=lambda _u: ex)
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "settings": {"band": "1", "levels": "10",
                               "size": "0.01", "bias": "auto"}})
        assert (await r.json())["decision"]["bias"] == 0            # ranging → neutral
        r2 = await c.post("/v1/grids", headers={"X-User-Id": "8"},
                          json={"market": "BTCUSDT", "settings": {"band": "1", "levels": "10",
                                "size": "0.01", "bias": "short"}})
        assert (await r2.json())["decision"]["bias"] == -1          # pinned short respected
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_rejects_grid_too_small_for_min_order():
    # The reported incident: 5.42 USDT on BTCUSDT. The venue minimum order (~0.001 BTC)
    # costs far more margin than the whole balance, so every order would be rejected.
    # Catch it at create time with a clear message, not a fake RUNNING grid.
    svc = AppService(client_factory=lambda _u: FakeExchange(
        {"mid": "65000", "equity": "5.42", "min_order_size": "0.001", "step": "0.001"}))
    c = await _client(build_worker_app(svc, "0"))
    try:
        r = await c.post("/v1/grids", headers={"X-User-Id": "7"},
                         json={"market": "BTCUSDT", "template": "safe"})
        assert r.status == 400
        assert "minimum order" in (await r.text()).lower()
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
