"""SENSE — build a RegimeFingerprint by fusing venue microstructure with external
signals (Elfa real-time, Nansen smart-money). (docs/CONCEPT.md §3 step 1)"""
from __future__ import annotations

import asyncio

from ..domain.models import RegimeFingerprint


async def classify_regime(exchange, market: str, signals: list | None = None) -> RegimeFingerprint:
    bid, ask = await exchange.best_bid_ask(market)
    mid = (bid + ask) / 2
    spread = float(ask - bid)
    range_width = (spread / float(mid)) if mid else 0.0

    realized_vol = 0.0
    trend = 0.0
    funding = 0.0
    vol_z = 0.0
    smart_money = 0.0
    social = 0.0
    # Fan out the signals in parallel — total latency is the slowest one, not the
    # sum. A signal that errors fails soft (it is skipped, never sinks the fusion).
    snaps = await asyncio.gather(*(s.snapshot(market) for s in (signals or [])), return_exceptions=True)
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        realized_vol = max(realized_vol, float(snap.get("realized_vol", 0.0)))
        trend += float(snap.get("trend_strength", 0.0))
        funding += float(snap.get("funding_rate", 0.0))
        vol_z += float(snap.get("volume_z", 0.0))
        smart_money += float(snap.get("smart_money_flow", 0.0))
        social += float(snap.get("social_momentum", 0.0))

    return RegimeFingerprint(
        realized_vol=realized_vol,
        trend_strength=trend,
        funding_rate=funding,
        range_width=range_width,
        volume_z=vol_z,
        smart_money_flow=smart_money,
        social_momentum=social,
    )
