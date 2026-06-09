"""Bybit fill-stream gap recovery — REST backfill after a WS reconnect, execId
dedup, backwards pagination. Offline: `_request` is stubbed (no keys/network)."""
import pytest

from perpsagent.adapters.exchanges.bybit.adapter import BybitExchange


def _ex() -> BybitExchange:
    return BybitExchange({"api_key": "k", "api_secret": "s", "testnet": True})


def _row(exec_id: str, link: str, ts: int, side: str = "Buy", price: str = "99") -> dict:
    return {"execId": exec_id, "orderLinkId": link, "symbol": "BTCUSDT", "side": side,
            "execPrice": price, "execQty": "0.01", "execTime": str(ts)}


def test_dedup_remembers_and_bounds():
    ex = _ex()
    assert ex._dedup("e1") is False     # first sighting passes through
    assert ex._dedup("e1") is True      # redelivery is swallowed
    assert ex._dedup("") is False       # no id -> never block the fill
    for i in range(5000):               # bounded memory: oldest ids age out
        ex._dedup(f"x{i}")
    assert len(ex._seen_execs) <= 4096


@pytest.mark.asyncio
async def test_missed_fills_sorted_skips_foreign_and_dedups():
    ex = _ex()
    rows = [  # newest-first, as /v5/execution/list returns
        _row("e3", "grid-i1-L2-3", 3000, side="Sell", price="101"),
        _row("e2", "not-a-grid-order", 2000),     # manual trade -> not ours
        _row("e1", "grid-i1-L1-1", 1000),
    ]

    async def fake_request(method, path, payload):
        assert path == "/v5/execution/list"
        return {"retCode": 0, "result": {"list": rows}}

    ex._request = fake_request  # type: ignore[assignment]
    fills = await ex._missed_fills(1)
    assert [f.external_id for f in fills] == ["grid-i1-L1-1", "grid-i1-L2-3"]  # oldest first
    assert await ex._missed_fills(1) == []     # every execId remembered -> no double-count


@pytest.mark.asyncio
async def test_missed_fills_pages_backwards_on_full_page():
    ex = _ex()
    calls: list[str] = []
    page1 = [_row(f"p1-{i}", f"grid-i1-L0-{i}", 5000 - i) for i in range(100)]  # 5000..4901
    page2 = [_row("old-1", "grid-i1-L9-999", 1500, side="Sell", price="101")]

    async def fake_request(method, path, payload):
        calls.append(payload)
        return {"retCode": 0, "result": {"list": page2 if "endTime" in payload else page1}}

    ex._request = fake_request  # type: ignore[assignment]
    fills = await ex._missed_fills(1000)
    assert len(fills) == 101
    assert fills[0].external_id == "grid-i1-L9-999"  # the older page surfaced, oldest first
    assert "endTime=4900" in calls[1]                 # paged strictly backwards in time


@pytest.mark.asyncio
async def test_backfill_noop_on_first_connect_and_recovers_after():
    ex = _ex()

    async def fake_request(method, path, payload):
        return {"retCode": 0, "result": {"list": [_row("e9", "grid-i1-L4-7", 9_999_999_999_999)]}}

    ex._request = fake_request  # type: ignore[assignment]
    assert await ex._backfill() == []        # first connect: just starts the watermark
    assert ex._last_exec_ms > 0
    missed = await ex._backfill()            # reconnect: the gap is recovered
    assert [f.external_id for f in missed] == ["grid-i1-L4-7"]
    assert ex._last_exec_ms == 9_999_999_999_999  # watermark advanced to the newest fill


@pytest.mark.asyncio
async def test_backfill_failure_never_kills_the_fresh_connection():
    ex = _ex()
    ex._last_exec_ms = 1000  # pretend we were connected before

    async def fake_request(method, path, payload):
        raise RuntimeError("rest down")

    ex._request = fake_request  # type: ignore[assignment]
    assert await ex._backfill() == []  # logged, not raised
