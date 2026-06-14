"""Wizard input parser — pure (str -> value | ValueError), tested offline."""
import pytest

from perpsbot.wizard import parse_market


def test_parse_market_normalizes_and_rejects():
    assert parse_market(" btcusdt ") == "BTCUSDT"
    for bad in ("", "BTC USDT", "X" * 21):
        with pytest.raises(ValueError):
            parse_market(bad)
