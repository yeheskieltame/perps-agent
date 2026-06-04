"""Pure-helper tests for the iZiSwap (Mantle DEX) adapter + registry wiring."""
from decimal import Decimal

from perpsagent.adapters.exchanges.mantle_dex.adapter import (
    decimal_price_to_point,
    point_to_decimal_price,
    round_to_point_delta,
    sell_amount_raw,
    side_mapping,
)
from perpsagent.adapters.exchanges.registry import make_exchange
from perpsagent.domain.models import Side, Venue

LOW = "0x" + "11" * 20
HIGH = "0x" + "22" * 20


def test_point_zero_when_undecimal_one():
    # equal decimals + price 1.0 => undecimal 1.0 => point 0
    assert decimal_price_to_point(1.0, True, 6, 6) == 0


def test_price_point_roundtrip_base_is_x():
    for price in (0.25, 0.5, 1.0, 3.7):
        pt = decimal_price_to_point(price, True, 18, 6)
        back = point_to_decimal_price(pt, True, 18, 6)
        assert abs(back - price) / price < 1e-3


def test_price_point_roundtrip_base_is_y():
    for price in (1500.0, 2000.0, 0.8):
        pt = decimal_price_to_point(price, False, 8, 18)
        back = point_to_decimal_price(pt, False, 8, 18)
        assert abs(back - price) / price < 1e-3


def test_round_to_point_delta():
    assert round_to_point_delta(283, 10, False) == 280
    assert round_to_point_delta(283, 10, True) == 290
    assert round_to_point_delta(-283, 10, False) == -290  # floor
    assert round_to_point_delta(-283, 10, True) == -280   # ceil
    assert round_to_point_delta(280, 10, False) == 280


def test_side_mapping_base_lower_address():
    sell, earn, sxey, up = side_mapping(Side.SELL, LOW, HIGH)
    assert sell == LOW and earn == HIGH and sxey is True and up is False
    sell, earn, sxey, up = side_mapping(Side.BUY, LOW, HIGH)
    assert sell == HIGH and earn == LOW and sxey is False and up is True


def test_side_mapping_base_higher_address():
    sell, earn, sxey, up = side_mapping(Side.SELL, HIGH, LOW)
    assert sxey is False and up is True  # selling the higher-address token


def test_sell_amount_raw():
    assert sell_amount_raw(Side.SELL, Decimal("1.5"), 0.0, 18, 6) == 1_500_000_000_000_000_000
    assert sell_amount_raw(Side.BUY, Decimal("2"), 0.5, 18, 6) == 1_000_000  # 2 * 0.5 * 1e6 quote


def test_registry_builds_mantle_dex_offline():
    ex = make_exchange(Venue.MANTLE_DEX, {"rpc_url": "http://localhost", "private_key": "0x" + "11" * 32, "markets": {}})
    assert ex.venue == "mantle_dex"
