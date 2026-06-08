"""Multi-tenant AppService: per-user clients, isolation, ownership, fill routing.
All on the in-memory FakeExchange (no keys/network)."""
import asyncio
from decimal import Decimal

import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.domain.models import GridConfig, Spacing, Venue


def cfg(instance: str) -> GridConfig:
    return GridConfig(instance, Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


async def _drain():
    for _ in range(100):
        await asyncio.sleep(0)


def _svc_two_users():
    clients = {
        1: FakeExchange({"mid": "100", "tick": "0.1", "equity": "1000"}),
        2: FakeExchange({"mid": "100", "tick": "0.1", "equity": "2000"}),
    }
    return AppService(client_factory=lambda uid: clients[uid]), clients


@pytest.mark.asyncio
async def test_status_isolated_per_user():
    svc, _ = _svc_two_users()
    await svc.create_grid(1, cfg("a1"))
    await svc.create_grid(2, cfg("b1"))
    assert [v.instance_id for v in await svc.status(1)] == ["a1"]
    assert [v.instance_id for v in await svc.status(2)] == ["b1"]
    assert await svc.status(999) == []          # unknown user → nothing
    await svc.disconnect(1)
    await svc.disconnect(2)


@pytest.mark.asyncio
async def test_balance_uses_each_users_own_client():
    svc, _ = _svc_two_users()
    assert (await svc.balance(1)).equity == Decimal("1000")
    assert (await svc.balance(2)).equity == Decimal("2000")


@pytest.mark.asyncio
async def test_ownership_enforced_across_users():
    svc, _ = _svc_two_users()
    await svc.create_grid(1, cfg("a1"))
    with pytest.raises(PermissionError):
        await svc.stop_grid(2, "a1")            # not user 2's instance
    with pytest.raises(PermissionError):
        await svc.pause_grid(2, "a1")
    await svc.stop_grid(1, "a1")                 # owner can stop
    await svc.disconnect(1)


@pytest.mark.asyncio
async def test_fills_route_only_to_owning_user():
    svc, clients = _svc_two_users()
    await svc.create_grid(1, cfg("a1"))
    await svc.create_grid(2, cfg("b1"))

    await clients[1].move_price("BTCUSDT", Decimal("94"))   # fills happen on user 1's venue only
    await _drain()

    ea = svc._sessions[1].manager.get("a1")
    eb = svc._sessions[2].manager.get("b1")
    assert ea.fill_count > 0          # routed to the owner's engine
    assert eb.fill_count == 0         # user 2 never sees user 1's fills (no fan-out)
    await svc.disconnect(1)
    await svc.disconnect(2)


@pytest.mark.asyncio
async def test_single_shared_exchange_still_supported():
    ex = FakeExchange({"mid": "100"})
    svc = AppService(ex)                          # legacy single-account mode
    iid = await svc.create_grid(7, cfg("i1"))
    assert [v.instance_id for v in await svc.status(7)] == ["i1"]
    assert (await svc.balance(7)).equity > 0
    await svc.stop_grid(7, iid)


def test_requires_exchange_or_factory():
    with pytest.raises(ValueError):
        AppService()
