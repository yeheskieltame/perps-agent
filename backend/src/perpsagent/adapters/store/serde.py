"""Shared (store-agnostic) serialization for persisted grid state.

Both SqliteStore and PostgresStore round-trip a GridConfig through the same JSON
shape, so the encoding lives here once and is unit-tested independently of any DB.
"""
from __future__ import annotations

import json
from decimal import Decimal

from ...domain.models import GridConfig, Spacing, Venue


def cfg_to_json(c: GridConfig) -> str:
    return json.dumps({
        "instance_id": c.instance_id, "venue": c.venue.value, "market": c.market,
        "lower": str(c.lower), "upper": str(c.upper), "levels": c.levels,
        "order_size": str(c.order_size), "spacing": c.spacing.value,
        "leverage": str(c.leverage), "max_levels": c.max_levels,
        "policy_version": c.policy_version, "bias": c.bias,
    })


def json_to_cfg(s: str) -> GridConfig:
    d = json.loads(s)
    return GridConfig(
        instance_id=d["instance_id"], venue=Venue(d["venue"]), market=d["market"],
        lower=Decimal(d["lower"]), upper=Decimal(d["upper"]), levels=int(d["levels"]),
        order_size=Decimal(d["order_size"]), spacing=Spacing(d["spacing"]),
        leverage=Decimal(d["leverage"]), max_levels=int(d["max_levels"]),
        policy_version=d["policy_version"], bias=int(d.get("bias", 0)),
    )
