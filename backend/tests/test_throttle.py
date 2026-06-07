"""Bybit client-side pacing + retry classification (deterministic, no wall clock)."""
import pytest

from perpsagent.adapters.exchanges.bybit.throttle import RateLimiter, is_retryable


def test_is_retryable_codes():
    assert is_retryable(429, 0)            # HTTP rate limit
    assert is_retryable(503, 0)            # HTTP server error
    assert is_retryable(200, 10006)        # per-UID rate limit
    assert is_retryable(200, 10018)        # IP rate limit
    assert is_retryable(200, 10002)        # timestamp expired (re-sign + retry)
    assert not is_retryable(200, 0)        # success
    assert not is_retryable(200, 110072)   # duplicate orderLinkId — permanent
    assert not is_retryable(400, 10001)    # bad request — permanent


@pytest.mark.asyncio
async def test_rate_limiter_paces_after_burst():
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)
        now[0] += d  # advance virtual time so the bucket refills

    rl = RateLimiter(rate=10.0, burst=2.0, clock=lambda: now[0], sleep=fake_sleep)
    await rl.acquire()
    await rl.acquire()
    assert slept == []                       # burst of 2 is free
    await rl.acquire()
    assert abs(sum(slept) - 0.1) < 1e-9      # 3rd waits 1/rate = 0.1s
    await rl.acquire()
    assert abs(sum(slept) - 0.2) < 1e-9      # 4th waits another 0.1s


@pytest.mark.asyncio
async def test_rate_limiter_zero_disables():
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    rl = RateLimiter(rate=0, clock=lambda: 0.0, sleep=fake_sleep)
    for _ in range(50):
        await rl.acquire()
    assert slept == []                       # disabled — never blocks
