"""Pure-parser tests for Elfa + Nansen signal clients, and regime fusion."""
import math

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.signals import base_symbol
from perpsagent.adapters.signals.elfa import parse_trending
from perpsagent.adapters.signals.nansen import parse_netflow
from perpsagent.agent.sense import classify_regime


def test_base_symbol():
    assert base_symbol("BTCUSDT") == "BTC"
    assert base_symbol("ETH-PERP") == "ETH"
    assert base_symbol("SOLUSDC") == "SOL"


def test_parse_netflow_sums_matching_rows_and_bounds():
    payload = {"data": [
        {"token_symbol": "BTC", "net_flow_24h_usd": 5_000_000},
        {"token_symbol": "BTC", "net_flow_24h_usd": -1_000_000},
        {"token_symbol": "ETH", "net_flow_24h_usd": 9_000_000},
    ]}
    out = parse_netflow(payload, "BTC")
    assert abs(out["smart_money_flow"] - math.tanh(4_000_000 / 1e7)) < 1e-9
    assert parse_netflow(payload, "DOGE")["smart_money_flow"] == 0.0
    assert parse_netflow({}, "BTC")["smart_money_flow"] == 0.0


def test_parse_trending_change_and_missing():
    p = {"data": [{"token": "BTC", "change_percent": 250}, {"token": "ETH", "change_percent": 10}]}
    assert abs(parse_trending(p, "BTC")["social_momentum"] - math.tanh(2.5)) < 1e-9
    assert parse_trending(p, "XYZ")["social_momentum"] == 0.0


@pytest.mark.asyncio
async def test_classify_regime_fuses_signals():
    class Sig:
        async def snapshot(self, market):
            return {"smart_money_flow": 0.5, "social_momentum": 0.3}

    fp = await classify_regime(FakeExchange({"mid": "100"}), "BTCUSDT", [Sig()])
    assert fp.smart_money_flow == 0.5
    assert fp.social_momentum == 0.3
