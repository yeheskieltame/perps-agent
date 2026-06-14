"""Order qty MUST be aligned to the venue's qty step + min, or the venue rejects
every order and the grid lands 'RUNNING' with zero resting orders (the bug an
unaligned auto-size triggered). FakeExchange — no keys/network."""
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.engine import GridEngine
from perpsagent.domain.grid import quantize_qty
from perpsagent.domain.models import GridConfig, Spacing, Venue


def test_quantize_qty_floors_to_step_and_respects_min():
    assert quantize_qty(Decimal("0.0234567"), Decimal("0.001"), Decimal("0.001")) == Decimal("0.023")
    assert quantize_qty(Decimal("0.0009"), Decimal("0.001"), Decimal("0.001")) == Decimal("0.001")  # < min → min
    assert quantize_qty(Decimal("5.7"), Decimal("0.1"), Decimal("0.1")) == Decimal("5.7")
    assert quantize_qty(Decimal("1.23"), Decimal("0"), Decimal("0")) == Decimal("1.23")  # no step → unchanged


def _cfg(size: Decimal):
    return GridConfig("i1", Venue.FAKE, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                      size, Spacing.ARITHMETIC)


@pytest.mark.asyncio
async def test_engine_aligns_order_size_to_step_on_start():
    ex = FakeExchange({"mid": "100", "tick": "0.1", "step": "0.001", "min_order_size": "0.001"})
    cfg = _cfg(Decimal("0.0234567"))                 # not a multiple of step 0.001
    await GridEngine(ex, cfg).start()
    assert cfg.order_size == Decimal("0.023")        # floored → orders are valid


@pytest.mark.asyncio
async def test_engine_bumps_sub_min_size_to_the_minimum():
    ex = FakeExchange({"mid": "100", "tick": "0.1", "step": "0.001", "min_order_size": "0.001"})
    cfg = _cfg(Decimal("0.0004"))                    # below the venue minimum
    await GridEngine(ex, cfg).start()
    assert cfg.order_size == Decimal("0.001")
