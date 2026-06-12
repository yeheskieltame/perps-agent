"""SENSE — build a RegimeFingerprint by fusing venue microstructure with external
signals (Elfa real-time, Nansen smart-money). (docs/CONCEPT.md §3 step 1)

External providers can be blind on a market (Surf has no HYPE feed -> trend=0/
vol=0) or DEAF to its trend (vol!=0 with trend=0 during a slow grind — the
2026-06-11 mainnet failure on three markets), and either way the agent silently
locks into symmetric mean-reversion while the market trends. The venue's own
klines are therefore always fused in: every venue can price its own candles, so
the regime read never goes dark, and the stronger trend signal wins."""
from __future__ import annotations

import asyncio
import math

from ..domain.models import RegimeFingerprint


_TF_MINUTES = {"D": 1440.0, "W": 10080.0, "M": 43200.0}


def tf_minutes(timeframe: str) -> float:
    """Bar size in minutes for a Bybit interval code ('1','5','60','D', ...)."""
    return _TF_MINUTES.get(str(timeframe).upper()) or float(timeframe)


def _tstat(px: list) -> float:
    """Drift/noise t-stat of log returns, clipped to [-1, 1]. A persistent move
    reads near ±1, chop reads near 0 (same scale as the external trend_strength,
    so the policy's bias threshold applies unchanged)."""
    rets = [math.log(b / a) for a, b in zip(px, px[1:])]
    n = len(rets)
    if n < 5:
        return 0.0
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / n)
    if sd == 0:
        return 0.0
    return max(-1.0, min(1.0, (mean * n) / (sd * math.sqrt(n))))


def _block_means(px: list, size: int) -> list:
    """Resample by averaging consecutive blocks. Averaging (vs subsampling) keeps
    every bar's information and shrinks per-point noise by sqrt(size); an EVEN
    size makes period-2 chop cancel exactly instead of aliasing into a fake
    drift (the 1/sqrt(n) floor of an alternating series on a short window)."""
    return [sum(px[i:i + size]) / size for i in range(0, len(px) - size + 1, size)]


def local_trend_vol(closes: list, bar_minutes: float = 1.0) -> tuple[float, float]:
    """Trend + realized vol from bar closes (oldest -> newest), scale-free.

    trend: MULTI-WINDOW t-stat — the raw series, the most recent half, and a
    4-bar block-mean resample; the strongest (by magnitude) wins. One window
    misses what another catches: a fresh break lives in the recent half, while a
    slow grind (staircase steps inside the bar noise) only clears the noise once
    4 bars of drift accumulate per block-mean return (live incident 2026-06-11:
    three markets ground 2-6% while the single-window read stayed "ranging").
    vol: stdev of per-bar log returns, daily-ized for the bar size — the same
    market reads roughly the same daily vol whether sensed on 1m or 1h bars."""
    px = [float(c) for c in closes if float(c) > 0]
    if len(px) < 10:
        return 0.0, 0.0
    rets = [math.log(b / a) for a, b in zip(px, px[1:])]
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / n)
    trend = max((_tstat(w) for w in (px, px[len(px) // 2:], _block_means(px, 4))), key=abs)
    return trend, sd * math.sqrt(1440.0 / max(bar_minutes, 1e-9))


async def classify_regime(exchange, market: str, signals: list | None = None,
                          timeframe: str = "1") -> RegimeFingerprint:
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

    # Venue klines fuse in ALWAYS, not only when the externals are fully blind.
    # Live incident 2026-06-11: externals reported vol!=0 with trend=0 on three
    # slowly-grinding markets, so the old both-zero gate skipped this read and the
    # grid stayed symmetric against a 6% grind until the breaker. The venue's own
    # candles are ground truth for trend — the stronger signal (by magnitude) wins.
    # The sensor reads the USER'S timeframe — the grid must sense the structure
    # on the bars where its pattern actually lives (live 2026-06-12: a 1m sensor
    # on a 1h structure read micro-noise as regime shifts and flapped the bias).
    range_position = 0.5
    kl = getattr(exchange, "klines", None)
    if kl is not None:
        try:
            try:
                closes = await kl(market, timeframe, 240)  # 240 bars of structure
            except TypeError:                              # ports with a (market)-only signature
                closes = await kl(market)
            local_trend, local_vol = local_trend_vol(closes, bar_minutes=tf_minutes(timeframe))
            trend = max(trend, local_trend, key=abs)
            realized_vol = max(realized_vol, local_vol)
            # Where does the CURRENT price sit in the window's band? Launching
            # with center=price at a range extreme is how "buy the dip" buys
            # the structural top (live 2026-06-12, LAB ep 2).
            px = [float(c) for c in closes if float(c) > 0]
            if px and max(px) > min(px):
                range_position = (px[-1] - min(px)) / (max(px) - min(px))
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
        range_position=range_position,
    )
