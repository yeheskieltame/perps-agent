"""Mantle DEX adapter — on-chain spot grid via iZiSwap (iZUMi) limit orders.

iZiSwap has native on-chain limit orders that map 1:1 to grid levels: a grid SELL
is a limit order selling base for quote at a point above mid; a grid BUY sells
quote for base at a point below mid. This makes the SAME grid engine run on-chain
on Mantle — literal end-to-end execution + real Mantle DeFi integration
(docs/CONCEPT.md §5-6).

Price model (iZiSwap): the pool discretizes price into integer **points** where
`undecimal_price_X_by_Y = 1.0001 ** point`, tokenX is the lower-address token, and
order points must be multiples of the pool `pointDelta`. The pure helpers below do
the decimal<->point conversion (with token decimals) and side mapping; they are
unit-tested. The web3 order flow targets the iZiSwap LimitOrderManager.

Mantle (chainId 5000) deployed addresses are the defaults; override via config.
Live use needs a funded Mantle wallet + token approvals; the on-chain call/return
shapes (esp. getActiveOrders) should be confirmed against the deployed contract.
"""
from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from typing import Any, AsyncIterator, Sequence

from ....domain.models import BalanceView, Fill, MarketMeta, Order, Position, Side

# iZiSwap on Mantle mainnet (chainId 5000) — developer.izumi.finance deployed contracts
MANTLE_LIMIT_ORDER_MANAGER = "0xcA7e21764CD8f7c1Ec40e651E25Da68AeD096037"
MANTLE_FACTORY = "0x45e5F26451CDB01B0fA1f8582E0aAD9A6F27C218"

_LN_1_0001 = math.log(1.0001)


# ---------- pure helpers (no I/O; unit-tested) ----------

def decimal_price_to_point(price: float, base_is_x: bool, dec_base: int, dec_quote: int) -> int:
    """Convert a human price (quote per base) to the pool point. base_is_x = base
    token has the lower address (so base == tokenX)."""
    p = float(price)
    if base_is_x:
        undecimal = p * (10 ** (dec_quote - dec_base))  # price_X_by_Y = Y per X
    else:
        undecimal = (1.0 / p) * (10 ** (dec_base - dec_quote))
    return round(math.log(undecimal) / _LN_1_0001)


def point_to_decimal_price(point: int, base_is_x: bool, dec_base: int, dec_quote: int) -> float:
    """Inverse of decimal_price_to_point → human price (quote per base)."""
    undecimal = 1.0001 ** point
    if base_is_x:
        return undecimal * (10 ** (dec_base - dec_quote))
    return (10 ** (dec_base - dec_quote)) / undecimal


def round_to_point_delta(point: int, point_delta: int, round_up: bool) -> int:
    if round_up:
        return -((-point) // point_delta) * point_delta
    return (point // point_delta) * point_delta


def side_mapping(side: Side, base_addr: str, quote_addr: str) -> tuple[str, str, bool, bool]:
    """Returns (sell_token, earn_token, sell_x_earn_y, round_up).
    SELL base -> sell base/earn quote; BUY base -> sell quote/earn base.
    sell_x_earn_y is True when selling tokenX (the lower-address token); per
    iZiSwap the sell point rounds DOWN when sell<earn, else UP."""
    if side is Side.SELL:
        sell, earn = base_addr, quote_addr
    else:
        sell, earn = quote_addr, base_addr
    sell_x_earn_y = sell.lower() < earn.lower()
    return sell, earn, sell_x_earn_y, (not sell_x_earn_y)


def sell_amount_raw(side: Side, qty_base: Decimal, price: float, dec_base: int, dec_quote: int) -> int:
    """Raw (undecimal) amount of the SELL token for the order."""
    if side is Side.SELL:  # selling base
        return int(Decimal(qty_base) * (Decimal(10) ** dec_base))
    # buying base = selling quote
    return int(Decimal(qty_base) * Decimal(str(price)) * (Decimal(10) ** dec_quote))


# ---------- minimal ABIs ----------

_ERC20_ABI = [
    {"name": "decimals", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"name": "balanceOf", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "approve", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"name": "allowance", "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]
_FACTORY_ABI = [
    {"name": "pool", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}], "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
_POOL_ABI = [
    {"name": "state", "inputs": [], "outputs": [
        {"type": "uint160"}, {"type": "int24"}, {"type": "uint16"}, {"type": "uint16"},
        {"type": "uint16"}, {"type": "bool"}, {"type": "uint128"}, {"type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"name": "pointDelta", "inputs": [], "outputs": [{"type": "int24"}], "stateMutability": "view", "type": "function"},
]
# AddLimOrderParam tuple: (tokenX, tokenY, fee, pt, amount, sellXEarnY, deadline)
_LOM_ABI = [
    {"name": "newLimOrder", "stateMutability": "payable", "type": "function",
     "inputs": [{"type": "uint256", "name": "idx"},
                {"type": "tuple", "name": "addLimitOrderParam", "components": [
                    {"type": "address", "name": "tokenX"}, {"type": "address", "name": "tokenY"},
                    {"type": "uint24", "name": "fee"}, {"type": "int24", "name": "pt"},
                    {"type": "uint128", "name": "amount"}, {"type": "bool", "name": "sellXEarnY"},
                    {"type": "uint256", "name": "deadline"}]}],
     "outputs": [{"type": "uint128"}, {"type": "uint128"}]},
    {"name": "decLimOrder", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "uint256"}, {"type": "uint128"}, {"type": "uint256"}], "outputs": [{"type": "uint128"}]},
    {"name": "collectLimOrder", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "address"}, {"type": "uint256"}, {"type": "uint128"}, {"type": "uint128"}],
     "outputs": [{"type": "uint128"}, {"type": "uint128"}]},
    {"name": "getDeactiveSlot", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]


class MantleDexExchange:
    """Implements ExchangePort against iZiSwap on Mantle. See domain.ports.ExchangePort."""

    venue = "mantle_dex"

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._rpc = config["rpc_url"]
        self._pk = config["private_key"]
        self._lom_addr = config.get("limit_order_manager", MANTLE_LIMIT_ORDER_MANAGER)
        self._factory_addr = config.get("factory", MANTLE_FACTORY)
        # markets: { "WMNTUSDT": {base, quote, base_decimals, quote_decimals, fee} }
        self._markets: dict[str, dict] = config.get("markets", {})
        self._w3 = None
        self._acct = None

    def _setup(self):
        if self._w3 is None:
            from eth_account import Account
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc))
            self._acct = Account.from_key(self._pk)
            self._factory = self._w3.eth.contract(address=Web3.to_checksum_address(self._factory_addr), abi=_FACTORY_ABI)
            self._lom = self._w3.eth.contract(address=Web3.to_checksum_address(self._lom_addr), abi=_LOM_ABI)
        return self._w3

    def _market(self, market: str) -> dict:
        m = self._markets.get(market)
        if m is None:
            raise KeyError(f"market {market} not configured (need base/quote addrs + decimals + fee)")
        return m

    def _pool(self, market: str):
        from web3 import Web3

        m = self._market(market)
        x, y = sorted([m["base"], m["quote"]], key=str.lower)
        addr = self._factory.functions.pool(
            Web3.to_checksum_address(x), Web3.to_checksum_address(y), int(m["fee"])
        ).call()
        return self._w3.eth.contract(address=Web3.to_checksum_address(addr), abi=_POOL_ABI), m

    async def market_meta(self, market: str) -> MarketMeta:
        return await asyncio.to_thread(self._market_meta_sync, market)

    def _market_meta_sync(self, market: str) -> MarketMeta:
        self._setup()
        pool, m = self._pool(market)
        pd = int(pool.functions.pointDelta().call())
        cp = int(pool.functions.state().call()[1])
        base_is_x = m["base"].lower() < m["quote"].lower()
        price = point_to_decimal_price(cp, base_is_x, m["base_decimals"], m["quote_decimals"])
        tick = Decimal(str(abs(price * (1.0001 ** pd - 1.0)))) or Decimal("0.0000001")
        return MarketMeta(market, tick, Decimal(str(m.get("min_order_size", "0.001"))), Decimal(str(m.get("min_order_size", "0.001"))))

    async def best_bid_ask(self, market: str) -> tuple[Decimal, Decimal]:
        return await asyncio.to_thread(self._best_bid_ask_sync, market)

    def _best_bid_ask_sync(self, market: str) -> tuple[Decimal, Decimal]:
        self._setup()
        pool, m = self._pool(market)
        cp = int(pool.functions.state().call()[1])
        base_is_x = m["base"].lower() < m["quote"].lower()
        price = Decimal(str(point_to_decimal_price(cp, base_is_x, m["base_decimals"], m["quote_decimals"])))
        return (price, price)

    async def place_order(self, order: Order) -> Order:
        return await asyncio.to_thread(self._place_sync, order)

    def _place_sync(self, order: Order) -> Order:
        from web3 import Web3

        self._setup()
        pool, m = self._pool(order.market)
        pd = int(pool.functions.pointDelta().call())
        base_is_x = m["base"].lower() < m["quote"].lower()
        dec_b, dec_q = m["base_decimals"], m["quote_decimals"]
        point = decimal_price_to_point(float(order.price), base_is_x, dec_b, dec_q)
        sell, earn, sell_x_earn_y, round_up = side_mapping(order.side, m["base"], m["quote"])
        point = round_to_point_delta(point, pd, round_up)
        amount = sell_amount_raw(order.side, order.qty, float(order.price), dec_b, dec_q)
        self._ensure_allowance(sell, amount)
        idx = int(self._lom.functions.getDeactiveSlot(self._acct.address).call())
        fee = int(m["fee"])
        x, y = sorted([m["base"], m["quote"]], key=str.lower)
        params = (Web3.to_checksum_address(x), Web3.to_checksum_address(y), fee, int(point),
                  int(amount), bool(sell_x_earn_y), int(time.time()) + 600)
        order.order_id = str(idx)
        self._send(self._lom.functions.newLimOrder(idx, params))
        return order

    def _ensure_allowance(self, token_addr: str, amount: int) -> None:
        from web3 import Web3

        erc20 = self._w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_ERC20_ABI)
        cur = erc20.functions.allowance(self._acct.address, Web3.to_checksum_address(self._lom_addr)).call()
        if cur < amount:
            self._send(erc20.functions.approve(Web3.to_checksum_address(self._lom_addr), 2**256 - 1))

    async def cancel_order(self, market: str, order_id: str) -> None:
        await asyncio.to_thread(self._cancel_sync, int(order_id))

    def _cancel_sync(self, idx: int) -> None:
        # decrease the whole remaining amount, then collect funds back
        self._send(self._lom.functions.decLimOrder(idx, 2**128 - 1, int(time.time()) + 600))
        self._send(self._lom.functions.collectLimOrder(self._acct.address, idx, 2**128 - 1, 2**128 - 1))

    async def set_leverage(self, market: str, leverage: Decimal) -> None:
        return None  # spot DEX: no leverage to set

    async def cancel_all(self, market: str) -> None:
        for o in await self.open_orders(market):
            if o.order_id is not None:
                await self.cancel_order(market, o.order_id)

    async def flatten(self, market: str) -> None:
        # spot grid: no perp position to market-close; cancel resting orders.
        await self.cancel_all(market)

    async def open_orders(self, market: str) -> Sequence[Order]:
        # TODO: parse LimitOrderManager.getActiveOrders(user) — confirm the on-chain
        # return struct against the deployed contract before relying on it live.
        return []

    async def balance(self) -> BalanceView:
        return await asyncio.to_thread(self._balance_sync)

    def _balance_sync(self) -> BalanceView:
        from web3 import Web3

        self._setup()
        # report the quote balance of the first configured market as equity
        if not self._markets:
            return BalanceView(Decimal(0), Decimal(0))
        m = next(iter(self._markets.values()))
        erc20 = self._w3.eth.contract(address=Web3.to_checksum_address(m["quote"]), abi=_ERC20_ABI)
        raw = erc20.functions.balanceOf(self._acct.address).call()
        bal = Decimal(raw) / (Decimal(10) ** m["quote_decimals"])
        return BalanceView(bal, bal)

    async def positions(self) -> Sequence[Position]:
        return []  # spot venue; inventory tracked by the engine

    async def stream_fills(self) -> AsyncIterator[Fill]:
        # On-chain has no push execution stream; a poller over getActiveOrders'
        # `filled` deltas emits Fills. TODO: implement once getActiveOrders parsing
        # is confirmed. Until then, yield nothing.
        raise NotImplementedError("TODO: poll getActiveOrders for filled deltas → Fill")
        yield  # pragma: no cover

    def _send(self, fn) -> str:
        signed = self._acct.sign_transaction(
            fn.build_transaction({
                "from": self._acct.address,
                "nonce": self._w3.eth.get_transaction_count(self._acct.address),
                "chainId": self._w3.eth.chain_id,
                "gasPrice": self._w3.eth.gas_price,
            })
        )
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = self._w3.eth.send_raw_transaction(raw)
        self._w3.eth.wait_for_transaction_receipt(h, timeout=120)
        return h.hex()
