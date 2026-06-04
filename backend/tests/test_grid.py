"""Pure grid-math tests (the only fully-implemented domain piece in the scaffold)."""
from decimal import Decimal

from perpsagent.domain.grid import arithmetic_levels, geometric_levels, quantize


def test_arithmetic_levels_endpoints_and_count():
    levels = arithmetic_levels(Decimal(100), Decimal(110), 11)
    assert len(levels) == 11
    assert levels[0] == Decimal(100)
    assert levels[-1] == Decimal(110)
    assert levels[1] == Decimal(101)


def test_geometric_levels_monotonic_and_bounds():
    levels = geometric_levels(Decimal(100), Decimal(200), 5)
    assert len(levels) == 5
    assert levels[0] == Decimal(100)
    assert abs(levels[-1] - Decimal(200)) < Decimal("0.000001")
    assert all(levels[i] < levels[i + 1] for i in range(len(levels) - 1))


def test_quantize_to_tick():
    assert quantize(Decimal("100.057"), Decimal("0.1")) == Decimal("100.1")
