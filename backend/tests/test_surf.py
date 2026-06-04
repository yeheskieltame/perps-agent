"""Surf.AI client tests — parsers match the REAL live response shapes."""
import pytest

from perpsagent.adapters.signals.surf import SurfSignals, first_value, rsi_to_trend


def test_first_value_real_shapes():
    rsi_payload = {"data": [{"name": "rsi", "symbol": "BTC", "interval": "1d",
                             "value": 18.416, "updated_at": 1780586646}], "meta": {"credits_used": 1}}
    assert first_value(rsi_payload) == 18.416
    funding_payload = {"data": [{"exchange": "binance", "pair": "BTC/USDT",
                                 "funding_rate": 0.00001519, "timestamp": 1780560000}]}
    assert first_value(funding_payload, "funding_rate") == 0.00001519
    assert first_value({"data": []}) is None
    assert first_value({}) is None
    assert first_value(None) is None


def test_rsi_to_trend():
    assert rsi_to_trend(75) == 0.5
    assert rsi_to_trend(50) == 0.0
    assert abs(rsi_to_trend(18.416) - (-0.63168)) < 1e-4


@pytest.mark.asyncio
async def test_surf_failsoft_without_key():
    assert await SurfSignals({}).snapshot("BTCUSDT") == {}
