"""Bybit UNIFIED balance mapping: `available` is the free USDT margin, read from the
USDT coin with a margin-first fallback — never the cross-coin total equity.
Offline: `_get` is stubbed so no keys/network are needed."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.bybit.adapter import BybitExchange


def _ex(account: dict) -> BybitExchange:
    ex = BybitExchange({"api_key": "k", "api_secret": "s", "testnet": True})

    async def fake_get(path, params):
        assert path == "/v5/account/wallet-balance"
        return {"list": [account]}

    ex._get = fake_get  # type: ignore[assignment]
    return ex


@pytest.mark.asyncio
async def test_available_is_usdt_free_margin_not_cross_coin_equity():
    # totalEquity is huge (other coins), but the % of margin must come from USDT.
    b = await _ex({
        "totalEquity": "999999", "totalAvailableBalance": "888888",
        "coin": [{"coin": "USDT", "walletBalance": "6000", "equity": "6010",
                  "availableToWithdraw": "5000"}],
    }).balance()
    assert b.available == Decimal("5000")   # USDT availableToWithdraw, not 888888
    assert b.equity == Decimal("6010")      # USDT equity, not 999999
    assert b.currency == "USDT"


@pytest.mark.asyncio
async def test_falls_back_to_usdt_wallet_when_available_is_blank():
    # Testnet UNIFIED quirk: availableToWithdraw "" → fall back to USDT walletBalance,
    # NOT totalEquity (which would over-size against other coins / unrealized PnL).
    b = await _ex({
        "totalEquity": "999999", "totalAvailableBalance": "",
        "coin": [{"coin": "USDT", "walletBalance": "6000", "equity": "",
                  "availableToWithdraw": ""}],
    }).balance()
    assert b.available == Decimal("6000")   # USDT wallet, not 999999
    assert b.equity == Decimal("6000")


@pytest.mark.asyncio
async def test_no_usdt_coin_yields_zero_available():
    b = await _ex({"totalEquity": "100", "totalAvailableBalance": "", "coin": []}).balance()
    assert b.available == Decimal("0")      # honest "too low", not a phantom balance
