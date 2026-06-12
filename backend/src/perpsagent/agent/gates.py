"""Launch gates — deploy-time checks that can veto or reshape an episode.

From the trader risk framework adopted 2026-06-12 (the "realistic edition"
doc), two gates the live sessions earned the hard way:

- funding: positive funding = longs PAY shorts. A biased grid that pays carry
  needs its trend edge to beat the carry, so it is demoted to symmetric.
  Extreme funding in either direction marks a crowded trade (squeeze risk) —
  the episode is skipped outright. Funding is never "free yield": a high rate
  is the market pricing the directional risk someone is paying to hold.
- news blackout: never launch into a scheduled macro window (CPI/FOMC/NFP).
  The market prices the EXPECTATION; the surprise is what moves, surprises are
  not predictable from a checklist, and a fresh grid eats the whipsaw.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

EXTREME_FUNDING = 0.001   # |rate| per 8h (0.1%) -> crowded trade, skip the episode
PAYING_FUNDING = 0.0003   # rate per 8h (0.03%) -> max carry a biased grid may pay
BLACKOUT_BEFORE = timedelta(minutes=30)
BLACKOUT_AFTER = timedelta(minutes=90)


class LaunchGated(Exception):
    """A launch gate vetoed this episode. `.reason` says why. Not deploying is a
    valid position — the caller should exit cleanly, never trade around it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def funding_gate(bias: int, funding: float) -> tuple[int, str]:
    """Gate a proposed grid bias against the current funding rate (per 8h,
    venue-raw fraction: 0.0001 == 0.01%/8h). Returns (gated_bias, note);
    raises LaunchGated when funding is extreme in either direction."""
    if abs(funding) >= EXTREME_FUNDING:
        raise LaunchGated(
            f"funding {funding * 100:+.3f}%/8h is extreme (crowded trade) — skip")
    pays = (bias > 0 and funding > PAYING_FUNDING) or (bias < 0 and funding < -PAYING_FUNDING)
    if pays:
        return 0, (f"bias {bias:+d} would pay funding {funding * 100:+.3f}%/8h "
                   f"(cap {PAYING_FUNDING * 100:.2f}%) — demoted to symmetric")
    return bias, ""


def parse_news_events(raw: str) -> list[datetime]:
    """Comma-separated ISO-8601 timestamps ('Z' accepted; naive = UTC).
    Example .env: PERPSAGENT_NEWS_EVENTS=2026-06-12T12:30:00Z,2026-06-18T18:00:00Z"""
    out: list[datetime] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        dt = datetime.fromisoformat(tok.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append(dt)
    return out


def news_blackout(now: datetime, events: list[datetime] | None) -> str | None:
    """Reason string when `now` falls inside any event's blackout window
    [-BLACKOUT_BEFORE, +BLACKOUT_AFTER], else None."""
    for ev in events or []:
        if ev - BLACKOUT_BEFORE <= now <= ev + BLACKOUT_AFTER:
            return (f"scheduled news at {ev.isoformat()} — inside blackout window "
                    f"[-{int(BLACKOUT_BEFORE.total_seconds() // 60)}m, "
                    f"+{int(BLACKOUT_AFTER.total_seconds() // 60)}m]")
    return None
