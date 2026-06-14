"""Postgres persistence — durable, concurrent multi-tenant store (StorePort +
owner-aware recovery + encrypted credential storage).

The production backend behind StorePort: many workers persist many users' grids and
fills concurrently (one shared rollback-free engine, real row-level concurrency),
where SQLite's single-writer lock would serialize everything. Same surface as
SqliteStore, so AppService/GridManager are unchanged — only the wiring picks this.

asyncpg is imported lazily (optional `postgres` extra), so this module loads even
when the dependency isn't installed; it's only needed once a DSN is actually used.
Prices/qtys are stored as TEXT to preserve Decimal exactness (same as SqliteStore).
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Sequence

from ...domain.models import Fill, GridConfig, Side
from .serde import cfg_to_json, json_to_cfg

_OPEN_STATES = ("INITIALIZING", "RUNNING", "REBALANCING")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    user_id BIGINT,
    venue TEXT, market TEXT, config TEXT, state TEXT, created_at BIGINT, regime TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id BIGSERIAL PRIMARY KEY,
    instance_id TEXT, market TEXT, side TEXT, price TEXT, qty TEXT,
    external_id TEXT, ts BIGINT, level INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fills_instance ON fills(instance_id);
CREATE TABLE IF NOT EXISTS credentials (
    user_id BIGINT PRIMARY KEY,
    ciphertext BYTEA NOT NULL,
    updated_at BIGINT
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY,
    settings TEXT NOT NULL,
    updated_at BIGINT
);
CREATE TABLE IF NOT EXISTS wallets (
    user_id BIGINT PRIMARY KEY,
    ciphertext BYTEA NOT NULL,
    updated_at BIGINT
);
"""


class PostgresStore:
    """Implements StorePort (+ owner recovery + CredentialStorePort) over asyncpg."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min, self._max = min_size, max_size
        self._pool = None

    async def _ensure(self):
        """Lazily create the connection pool + schema (idempotent)."""
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=self._min, max_size=self._max)
            async with self._pool.acquire() as conn:
                await conn.execute(_SCHEMA)
        return self._pool

    # ---- fills ----

    async def record_fill(self, fill: Fill) -> None:
        pool = await self._ensure()
        await pool.execute(
            "INSERT INTO fills(instance_id,market,side,price,qty,external_id,ts,level) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
            fill.instance_id, fill.market, fill.side.value, str(fill.price), str(fill.qty),
            fill.external_id, fill.ts, fill.level,
        )

    async def load_fills(self, instance_id: str) -> list[Fill]:
        pool = await self._ensure()
        rows = await pool.fetch(
            "SELECT instance_id,market,side,price,qty,external_id,ts,level FROM fills "
            "WHERE instance_id=$1 ORDER BY id",
            instance_id,
        )
        return [
            Fill(instance_id=r["instance_id"], market=r["market"], side=Side(r["side"]),
                 price=Decimal(r["price"]), qty=Decimal(r["qty"]), external_id=r["external_id"],
                 ts=int(r["ts"]), level=int(r["level"]))
            for r in rows
        ]

    # ---- instances ----

    async def save_instance(self, cfg: GridConfig, state: str = "RUNNING",
                            regime_json: str | None = None) -> None:
        pool = await self._ensure()
        await pool.execute(
            "INSERT INTO instances(instance_id,venue,market,config,state,created_at,regime) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT(instance_id) DO UPDATE SET state=EXCLUDED.state, config=EXCLUDED.config, "
            "regime=COALESCE(EXCLUDED.regime, instances.regime)",
            cfg.instance_id, cfg.venue.value, cfg.market, cfg_to_json(cfg), state,
            int(time.time()), regime_json,
        )

    async def set_state(self, instance_id: str, state: str) -> None:
        pool = await self._ensure()
        await pool.execute("UPDATE instances SET state=$1 WHERE instance_id=$2", state, instance_id)

    async def set_owner(self, instance_id: str, user_id: int) -> None:
        pool = await self._ensure()
        await pool.execute("UPDATE instances SET user_id=$1 WHERE instance_id=$2", user_id, instance_id)

    async def load_regime(self, instance_id: str) -> str | None:
        pool = await self._ensure()
        return await pool.fetchval("SELECT regime FROM instances WHERE instance_id=$1", instance_id)

    async def load_open_instances(self) -> Sequence[GridConfig]:
        pool = await self._ensure()
        rows = await pool.fetch("SELECT config FROM instances WHERE state = ANY($1::text[])", list(_OPEN_STATES))
        return [json_to_cfg(r["config"]) for r in rows]

    async def load_open_with_owner(self) -> list[tuple[int, GridConfig]]:
        pool = await self._ensure()
        rows = await pool.fetch(
            "SELECT user_id, config FROM instances WHERE state = ANY($1::text[]) AND user_id IS NOT NULL",
            list(_OPEN_STATES),
        )
        return [(int(r["user_id"]), json_to_cfg(r["config"])) for r in rows]

    # ---- credentials (CredentialStorePort) ----

    async def put_credentials(self, user_id: int, ciphertext: bytes) -> None:
        pool = await self._ensure()
        await pool.execute(
            "INSERT INTO credentials(user_id,ciphertext,updated_at) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET ciphertext=EXCLUDED.ciphertext, updated_at=EXCLUDED.updated_at",
            user_id, ciphertext, int(time.time()),
        )

    async def get_credentials(self, user_id: int) -> bytes | None:
        pool = await self._ensure()
        return await pool.fetchval("SELECT ciphertext FROM credentials WHERE user_id=$1", user_id)

    async def delete_credentials(self, user_id: int) -> None:
        pool = await self._ensure()
        await pool.execute("DELETE FROM credentials WHERE user_id=$1", user_id)

    # ---- managed MNT wallets (WalletStorePort) ----

    async def put_wallet(self, user_id: int, ciphertext: bytes) -> None:
        pool = await self._ensure()
        await pool.execute(
            "INSERT INTO wallets(user_id,ciphertext,updated_at) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET ciphertext=EXCLUDED.ciphertext, updated_at=EXCLUDED.updated_at",
            user_id, ciphertext, int(time.time()),
        )

    async def get_wallet(self, user_id: int) -> bytes | None:
        pool = await self._ensure()
        return await pool.fetchval("SELECT ciphertext FROM wallets WHERE user_id=$1", user_id)

    # ---- per-user strategy settings (JSON of validated knobs — app/prefs.py) ----

    async def put_settings(self, user_id: int, settings_json: str) -> None:
        pool = await self._ensure()
        await pool.execute(
            "INSERT INTO user_settings(user_id,settings,updated_at) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET settings=EXCLUDED.settings, updated_at=EXCLUDED.updated_at",
            user_id, settings_json, int(time.time()),
        )

    async def get_settings(self, user_id: int) -> str | None:
        pool = await self._ensure()
        return await pool.fetchval("SELECT settings FROM user_settings WHERE user_id=$1", user_id)

    async def delete_settings(self, user_id: int) -> None:
        pool = await self._ensure()
        await pool.execute("DELETE FROM user_settings WHERE user_id=$1", user_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
