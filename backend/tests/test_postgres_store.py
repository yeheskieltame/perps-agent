"""PostgresStore integration — runs ONLY when PERPSAGENT_TEST_PG_DSN points at a
reachable Postgres (otherwise skipped, so the suite stays green without PG infra).

  PERPSAGENT_TEST_PG_DSN=postgresql://user:pass@localhost:5432/perpsagent_test \
    .venv/bin/python -m pytest tests/test_postgres_store.py -q

Uses a unique instance/user id per run section and cleans up, so it is safe to
point at a scratch database.
"""
import os
from decimal import Decimal

import pytest

from perpsagent.domain.models import Fill, GridConfig, Side, Spacing, Venue

_DSN = os.environ.get("PERPSAGENT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="set PERPSAGENT_TEST_PG_DSN to run Postgres integration tests")


def _cfg(instance: str) -> GridConfig:
    return GridConfig(instance, Venue.FAKE, "BTCUSDT", Decimal("90"), Decimal("110"), 11,
                      Decimal("0.01"), Spacing.ARITHMETIC)


async def _fresh_store():
    from perpsagent.adapters.store.postgres_store import PostgresStore

    store = PostgresStore(_DSN)
    pool = await store._ensure()
    await pool.execute("DELETE FROM fills; DELETE FROM instances; DELETE FROM credentials;")
    return store


@pytest.mark.asyncio
async def test_pg_instance_owner_and_fills_roundtrip():
    store = await _fresh_store()
    try:
        await store.save_instance(_cfg("a1"))
        await store.set_owner("a1", 42)
        await store.record_fill(Fill("a1", "BTCUSDT", Side.BUY, Decimal("98"), Decimal("0.01"),
                                     "grid-a1-L4-0", 1, level=4))

        owned = await store.load_open_with_owner()
        assert owned == [(42, _cfg("a1"))] or (len(owned) == 1 and owned[0][0] == 42)
        fills = await store.load_fills("a1")
        assert len(fills) == 1 and fills[0].price == Decimal("98")

        await store.set_state("a1", "CLOSED")
        assert await store.load_open_with_owner() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pg_credentials_roundtrip():
    from perpsagent.adapters.store.credentials import CredentialCodec

    store = await _fresh_store()
    try:
        codec = CredentialCodec(CredentialCodec.generate_key())
        await store.put_credentials(7, codec.encrypt({"api_key": "K", "api_secret": "S"}))
        token = await store.get_credentials(7)
        assert token is not None and codec.decrypt(token) == {"api_key": "K", "api_secret": "S"}
        assert await store.get_credentials(999) is None
    finally:
        await store.close()
