"""Wizard input parsers — pure (str -> value | ValueError), tested offline."""
import pytest

from perpsbot.wizard import parse_band_pct, parse_levels, parse_market, parse_size


def test_parse_market_normalizes_and_rejects():
    assert parse_market(" btcusdt ") == "BTCUSDT"
    for bad in ("", "BTC USDT", "X" * 21):
        with pytest.raises(ValueError):
            parse_market(bad)


def test_parse_band_returns_fraction():
    assert parse_band_pct("1") == "0.01"
    assert parse_band_pct("±2%") == "0.02"
    assert parse_band_pct("0.5") == "0.005"
    for bad in ("0", "11", "abc"):       # 0 and >10 percent are out of range
        with pytest.raises(ValueError):
            parse_band_pct(bad)


def test_parse_levels_whole_in_range():
    assert parse_levels(" 10 ") == 10
    for bad in ("1", "201", "x", "3.5"):
        with pytest.raises(ValueError):
            parse_levels(bad)


def test_parse_size_positive_number():
    assert parse_size(" 0.001 ") == "0.001"
    for bad in ("0", "-1", "abc"):
        with pytest.raises(ValueError):
            parse_size(bad)
