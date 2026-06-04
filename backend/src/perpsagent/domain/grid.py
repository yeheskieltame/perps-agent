"""Pure grid logic. No I/O. Mirrors the deltaperps engine helpers.

Level math + order construction + fill pairing live here so the engine in
`app/` stays a thin async shell over the `ExchangePort`. Always quantize to the
market tickSize before placing (round half-down for BUYs, half-up for SELLs).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from .models import GridConfig, Order, Side, Spacing


def arithmetic_levels(lower: Decimal, upper: Decimal, n: int) -> list[Decimal]:
    if n < 2:
        raise ValueError("need >= 2 levels")
    step = (upper - lower) / (n - 1)
    return [lower + step * i for i in range(n)]


def geometric_levels(lower: Decimal, upper: Decimal, n: int) -> list[Decimal]:
    if n < 2:
        raise ValueError("need >= 2 levels")
    if lower <= 0:
        raise ValueError("lower must be > 0 for geometric spacing")
    ratio = (upper / lower) ** (Decimal(1) / Decimal(n - 1))
    return [lower * (ratio ** i) for i in range(n)]


def quantize(value: Decimal, tick: Decimal, side: Side | None = None) -> Decimal:
    """Snap a price to the tick grid. BUYs round down, SELLs round up so the
    quantized price never crosses to the wrong side of the intended level."""
    if tick <= 0:
        return value
    rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP if side is Side.SELL else ROUND_HALF_UP
    return (value / tick).quantize(Decimal(1), rounding=rounding) * tick


def plan_levels(cfg: GridConfig) -> list[Decimal]:
    if cfg.levels > cfg.max_levels:
        raise ValueError(f"levels {cfg.levels} exceeds max_levels {cfg.max_levels}")
    fn = geometric_levels if cfg.spacing is Spacing.GEOMETRIC else arithmetic_levels
    return fn(cfg.lower, cfg.upper, cfg.levels)


def _external_id(instance_id: str, level: int, nonce: int) -> str:
    return f"grid-{instance_id}-L{level}-{nonce}"


def build_grid_orders(cfg: GridConfig, levels: list[Decimal], mid: Decimal) -> list[Order]:
    """Initial grid: BUY at every level below mid, SELL at every level above.
    The level nearest mid is skipped to avoid an immediate self-cross."""
    orders: list[Order] = []
    for i, price in enumerate(levels):
        if price < mid:
            side = Side.BUY
        elif price > mid:
            side = Side.SELL
        else:
            continue
        orders.append(
            Order(
                instance_id=cfg.instance_id,
                market=cfg.market,
                side=side,
                price=price,
                qty=cfg.order_size,
                external_id=_external_id(cfg.instance_id, i, 0),
                level=i,
            )
        )
    return orders


def parse_level(external_id: str) -> int:
    # grid-{instance}-L{level}-{nonce}
    for part in external_id.split("-"):
        if part.startswith("L") and part[1:].isdigit():
            return int(part[1:])
    raise ValueError(f"cannot parse level from external_id: {external_id}")


def compute_paired_order(cfg: GridConfig, levels: list[Decimal], fill_level: int, fill_side: Side, nonce: int) -> Order | None:
    """On a BUY fill at level i, place a SELL one level up (i+1); on a SELL fill
    at level j, place a BUY one level down (j-1). Returns None at the boundary."""
    if fill_side is Side.BUY:
        target = fill_level + 1
        side = Side.SELL
    else:
        target = fill_level - 1
        side = Side.BUY
    if target < 0 or target >= len(levels):
        return None
    return Order(
        instance_id=cfg.instance_id,
        market=cfg.market,
        side=side,
        price=levels[target],
        qty=cfg.order_size,
        external_id=_external_id(cfg.instance_id, target, nonce),
        level=target,
    )


def parse_external_id(external_id: str) -> tuple[str, int, int]:
    """Reverse of _external_id: 'grid-{instance}-L{level}-{nonce}' ->
    (instance_id, level, nonce). Instance ids may contain hyphens; we split on
    the last '-L' segment."""
    if not external_id.startswith("grid-"):
        raise ValueError(f"not a grid external_id: {external_id}")
    body = external_id[len("grid-"):]
    instance_id, sep, tail = body.rpartition("-L")
    if not sep:
        raise ValueError(f"cannot parse level from: {external_id}")
    level_s, _, nonce_s = tail.partition("-")
    return instance_id, int(level_s), int(nonce_s or 0)
