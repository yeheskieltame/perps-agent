"""SENSE — build a RegimeFingerprint by fusing venue microstructure with external
signals (Elfa real-time, Nansen smart-money). (docs/CONCEPT.md §3 step 1)

When no external provider covers a market (e.g. Surf has no HYPE feed), the
fingerprint would read trend=0/vol=0 — a BLIND sensor that silently locks the
agent into symmetric mean-reversion while the market trends (the exact failure
seen live on mainnet). The venue's own klines are the fallback: every venue can
price its own candles, so the regime read never goes dark."""
from __future__ import annotations

import asyncio
import math

from ..domain.models import RegimeFingerprint


def local_trend_vol(closes: list) -> tuple[float, float]:
    """Trend + realized vol from 1-minute closes (oldest -> newest), scale-free.

    trend: drift/noise t-stat of log returns over the window, clipped to [-1, 1]
    — a persistent move reads near ±1, chop reads near 0 (same scale as the
    external trend_strength, so the ±0.4 bias threshold applies unchanged).
    vol: stdev of 1-minute log returns, daily-ized (sqrt of 1440 minutes)."""
    px = [float(c) for c in closes if float(c) > 0]
    if len(px) < 10:
        return 0.0, 0.0
    rets = [math.log(b / a) for a, b in zip(px, px[1:])]
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / n)
    if sd == 0:
        return 0.0, 0.0
    trend = max(-1.0, min(1.0, (mean * n) / (sd * math.sqrt(n))))
    return trend, sd * math.sqrt(1440.0)


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

    if trend == 0.0 and realized_vol == 0.0:  # external providers blind on this market
        kl = getattr(exchange, "klines", None)
        if kl is not None:
            try:
                trend, realized_vol = local_trend_vol(await kl(market))
            except Exception:  # noqa: BLE001 — a fallback must never sink the loop
                pass

    return RegimeFingerprint(
        realized_vol=realized_vol,
        trend_strength=trend,
        funding_rate=funding,
        range_width=range_width,
        volume_z=vol_z,
        smart_money_flow=smart_money,
        social_momentum=social,
    )
