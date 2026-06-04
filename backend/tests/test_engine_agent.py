"""End-to-end heartbeat: grid engine + FakeExchange + agent loop on in-memory chain."""
import asyncio
from decimal import Decimal

import pytest

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.exchanges.registry import make_exchange
from perpsagent.agent.loop import LearningLoop
from perpsagent.app.engine import GridEngine
from perpsagent.app.manager import GridManager
from perpsagent.app.service import AppService
from perpsagent.domain.models import Fill, GridConfig, Side, Spacing, Venue


def cfg(instance="i1", market="BTCUSDT"):
    return GridConfig(
        instance, Venue.FAKE, market, Decimal("90"), Decimal("110"), 11,
        Decimal("0.01"), Spacing.ARITHMETIC,
    )


async def _drain():
    for _ in range(100):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_engine_places_grid():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    orders = await eng.start()
    buys = [o for o in orders if o.side is Side.BUY]
    sells = [o for o in orders if o.side is Side.SELL]
    assert len(buys) == 5 and len(sells) == 5
    assert len(await ex.open_orders("BTCUSDT")) == 10


@pytest.mark.asyncio
async def test_fill_places_paired_order():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    pair = await eng.handle_fill(
        Fill("i1", "BTCUSDT", Side.BUY, Decimal("98"), Decimal("0.01"), "grid-i1-L4-0", 1, level=4)
    )
    assert pair is not None and pair.side is Side.SELL and pair.level == 5


@pytest.mark.asyncio
async def test_realized_pnl_roundtrip():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    await eng.handle_fill(Fill("i1", "BTCUSDT", Side.BUY, Decimal("98"), Decimal("0.01"), "grid-i1-L4-0", 1, level=4))
    await eng.handle_fill(Fill("i1", "BTCUSDT", Side.SELL, Decimal("100"), Decimal("0.01"), "grid-i1-L5-0", 2, level=5))
    assert eng.realized == Decimal("0.02")
    assert eng.winrate == 1.0


@pytest.mark.asyncio
async def test_fake_exchange_streams_fill():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    gen = ex.stream_fills()
    await ex.move_price("BTCUSDT", Decimal("96"))
    fill = await asyncio.wait_for(gen.__anext__(), 1)
    assert fill.side is Side.BUY


@pytest.mark.asyncio
async def test_registry_builds_fake():
    ex = make_exchange(Venue.FAKE, {})
    assert ex.venue == "fake"


@pytest.mark.asyncio
async def test_manager_consumes_stream():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    mgr = GridManager()
    eng = await mgr.create(ex, cfg())
    await ex.move_price("BTCUSDT", Decimal("94"))
    await _drain()
    assert eng.fill_count > 0
    await mgr.stop("i1")


@pytest.mark.asyncio
async def test_service_create_status_balance():
    ex = FakeExchange({"mid": "100"})
    svc = AppService(ex)
    iid = await svc.create_grid(7, cfg())
    st = await svc.status(7)
    assert len(st) == 1 and st[0].instance_id == "i1"
    assert (await svc.balance(7)).equity > 0
    await svc.stop_grid(7, iid)


@pytest.mark.asyncio
async def test_agent_loop_commit_execute_attest_learn():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})
    chain = MemoryChain()
    mgr = GridManager()
    loop = LearningLoop(ex, chain, mgr)

    iid, c, tx = await loop.plan_and_launch("BTCUSDT")
    assert tx.startswith("0x")
    assert iid in chain._commits  # committed BEFORE any trade

    await ex.move_price("BTCUSDT", Decimal("98"))   # fill buys
    await _drain()
    await ex.move_price("BTCUSDT", Decimal("103"))  # fill sells -> round-trips
    await _drain()

    outcome = await loop.close_and_learn(iid)
    assert iid in chain._attestations
    assert chain._memory  # a verified record was written back to memory
    assert outcome.fill_count > 0
