"""A grid that places ZERO orders must NOT report RUNNING — it raises
OrderPlacementError so the launch path surfaces *why* (insufficient margin / below
the venue's minimum order value) instead of showing a live grid that never traded.
This is the bug behind a Telegram grid stuck "RUNNING · fills 0" with no open
orders on Bybit. Covers both the single-order and the batch placement paths."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.app.safety import CircuitBreaker
from perpsagent.domain.models import (
    GridConfig, GridState, OrderPlacementError, Spacing, Venue,
)


def cfg() -> GridConfig:
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


class _RejectEach(FakeExchange):
    """Every single-order placement is rejected (e.g. Bybit 110007 'ab not enough')."""

    async def place_order(self, order):
        raise RuntimeError("bybit error 110007: ab not enough for new order")


class _RejectBatch(FakeExchange):
    """Exposes a batch endpoint that accepts nothing (order_id left unset)."""

    async def place_orders(self, orders):
        return list(orders)  # no order_id stamped → engine counts 0 accepted


@pytest.mark.asyncio
async def test_zero_orders_single_path_raises_and_halts():
    eng = GridEngine(_RejectEach({"mid": "100", "tick": "0.1"}), cfg())
    with pytest.raises(OrderPlacementError) as ei:
        await eng.start()
    assert eng.state is GridState.HALTED
    assert "0/" in str(ei.value) and "BTCUSDT" in str(ei.value)


@pytest.mark.asyncio
async def test_zero_orders_batch_path_raises():
    eng = GridEngine(_RejectBatch({"mid": "100", "tick": "0.1"}), cfg())
    with pytest.raises(OrderPlacementError):
        await eng.start()
    assert eng.state is GridState.HALTED


@pytest.mark.asyncio
async def test_partial_acceptance_still_runs():
    """If at least one order rests, the grid is genuinely live — no false alarm."""
    eng = GridEngine(FakeExchange({"mid": "100", "tick": "0.1"}), cfg())
    orders = await eng.start()
    assert eng.state is GridState.RUNNING and orders


@pytest.mark.asyncio
async def test_auto_inventory_cap_rescaled_to_quantized_size():
    """When the venue min bumps the size up, an auto cap (size×levels) is rescaled
    to the size that actually rests — else the breaker caps 1000x too small."""
    # requested 0.0001 is below the fake's min 0.001 → quantize_qty bumps to 0.001.
    c = GridConfig("i2", Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                   Decimal("0.0001"), Spacing.ARITHMETIC)
    breaker = CircuitBreaker(max_inventory=Decimal("0.0001") * c.levels)  # auto: size×levels
    eng = GridEngine(FakeExchange({"mid": "100", "tick": "0.1", "min_order_size": "0.001"}),
                     c, breaker=breaker)
    await eng.start()
    assert eng.cfg.order_size == Decimal("0.001")          # bumped to venue min
    assert breaker.max_inventory == Decimal("0.001") * c.levels  # cap follows the real size
