"""Bybit batch order placement — chunking, result parsing, partial-failure tolerance.
Offline: the signed HTTP layer (`_request`) is stubbed so no keys/network are needed."""
import json
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.bybit.adapter import BybitExchange, _chunked
from perpsagent.domain.models import Order, Side


def _ex() -> BybitExchange:
    return BybitExchange({"api_key": "k", "api_secret": "s", "testnet": True})


def _orders(n: int) -> list[Order]:
    return [
        Order(instance_id="i1", market="BTCUSDT", side=Side.BUY, price=Decimal("100") + i,
              qty=Decimal("0.01"), external_id=f"grid-i1-L{i}-{i}", level=i)
        for i in range(n)
    ]


def test_chunked():
    assert list(_chunked(list(range(25)), 20)) == [list(range(20)), list(range(20, 25))]
    assert list(_chunked([], 20)) == []


def test_order_payload_excludes_category_and_maps_fields():
    ex = _ex()
    p = ex._order_payload(Order("i1", "BTCUSDT", Side.SELL, Decimal("101"), Decimal("0.02"),
                                "grid-i1-L3-9", level=3))
    assert "category" not in p                       # category is top-level in batch
    assert p == {"symbol": "BTCUSDT", "side": "Sell", "orderType": "Limit",
                 "qty": "0.02", "price": "101", "timeInForce": "PostOnly",
                 "orderLinkId": "grid-i1-L3-9"}


@pytest.mark.asyncio
async def test_place_orders_chunks_and_assigns_ids():
    ex = _ex()
    calls: list[tuple[str, int]] = []

    async def fake_request(method, path, payload):
        body = json.loads(payload)
        reqs = body["request"]
        calls.append((path, len(reqs)))
        rows = [{"orderId": f"oid-{r['orderLinkId']}", "orderLinkId": r["orderLinkId"]} for r in reqs]
        ext = [{"code": 0, "msg": "OK"} for _ in reqs]
        if len(reqs) < 20:                           # last (small) chunk: reject its first order
            ext[0] = {"code": 110007, "msg": "insufficient balance"}
            rows[0] = {"orderId": "", "orderLinkId": reqs[0]["orderLinkId"]}
        return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}, "retExtInfo": {"list": ext}}

    ex._request = fake_request  # type: ignore[assignment]
    placed = await ex.place_orders(_orders(25))

    assert calls == [("/v5/order/create-batch", 20), ("/v5/order/create-batch", 5)]
    assert len(placed) == 25                          # every order accounted for
    assert placed[0].order_id == "oid-grid-i1-L0-0"   # success path got an id
    assert not placed[20].order_id                     # rejected order tolerated (no id), not dropped


@pytest.mark.asyncio
async def test_place_orders_tolerates_whole_batch_rejection():
    ex = _ex()

    async def fake_request(method, path, payload):
        return {"retCode": 10001, "retMsg": "params error", "result": {}, "retExtInfo": {}}

    ex._request = fake_request  # type: ignore[assignment]
    placed = await ex.place_orders(_orders(3))
    assert len(placed) == 3 and all(o.order_id is None for o in placed)  # logged, never crashes


@pytest.mark.asyncio
async def test_place_order_single_assigns_id():
    ex = _ex()

    async def fake_request(method, path, payload):
        assert path == "/v5/order/create"
        body = json.loads(payload)
        assert body["category"] == "linear" and body["orderLinkId"] == "grid-i1-L1-1"
        return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "single-1"}}

    ex._request = fake_request  # type: ignore[assignment]
    o = await ex.place_order(_orders(2)[1])
    assert o.order_id == "single-1"
