"""Engine uses the venue batch endpoint when present (one round-trip per grid lay),
and falls back to one-by-one otherwise — both keep ids globally unique."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.domain.models import GridConfig, Spacing, Venue


def cfg() -> GridConfig:
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


class _BatchFake(FakeExchange):
    """A venue that exposes place_orders (like Bybit's create-batch)."""

    def __init__(self, config=None):
        super().__init__(config)
        self.batch_calls = 0
        self.batch_sizes: list[int] = []

    async def place_orders(self, orders):
        self.batch_calls += 1
        self.batch_sizes.append(len(orders))
        return [await self.place_order(o) for o in orders]


@pytest.mark.asyncio
async def test_start_places_grid_in_one_batch():
    ex = _BatchFake({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    assert ex.batch_calls == 1 and ex.batch_sizes == [10]   # 10 orders, ONE round-trip
    ids = [o.external_id for o in await ex.open_orders("BTCUSDT")]
    assert len(ids) == 10 and len(set(ids)) == 10 and all(ids)  # unique, stamped


@pytest.mark.asyncio
async def test_recenter_replaces_grid_in_one_batch():
    ex = _BatchFake({"mid": "100", "tick": "0.1"})
    eng = GridEngine(ex, cfg())
    await eng.start()
    await ex.move_price("BTCUSDT", Decimal("130"))   # far above band [90,110]
    recentered = await eng.maybe_recenter()
    assert recentered is True
    assert ex.batch_calls == 2                         # start + one re-center batch


@pytest.mark.asyncio
async def test_fallback_one_by_one_without_batch_endpoint():
    ex = FakeExchange({"mid": "100", "tick": "0.1"})   # no place_orders attr
    eng = GridEngine(ex, cfg())
    await eng.start()
    ids = [o.external_id for o in await ex.open_orders("BTCUSDT")]
    assert len(ids) == 10 and len(set(ids)) == 10      # still placed, ids unique
