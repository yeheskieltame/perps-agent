"""SQLite persistence — implements StorePort (+ recovery helpers).

Durably records grid instances and fills so the engine can recover realized
state after a restart (production-grade; mirrors the deltaperps recovery
principle). Synchronous sqlite3 wrapped in asyncio.to_thread so the async engine
never blocks. Upgrade path: Postgres behind the same StorePort.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from decimal import Decimal
from typing import Sequence

from ...domain.models import Fill, GridConfig, Side, Spacing, Venue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    venue TEXT, market TEXT, config TEXT, state TEXT, created_at INTEGER, regime TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT, market TEXT, side TEXT, price TEXT, qty TEXT,
    external_id TEXT, ts INTEGER, level INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fills_instance ON fills(instance_id);
"""
_OPEN_STATES = ("INITIALIZING", "RUNNING", "REBALANCING")


def _cfg_to_json(c: GridConfig) -> str:
    return json.dumps({
        "instance_id": c.instance_id, "venue": c.venue.value, "market": c.market,
        "lower": str(c.lower), "upper": str(c.upper), "levels": c.levels,
        "order_size": str(c.order_size), "spacing": c.spacing.value,
        "leverage": str(c.leverage), "max_levels": c.max_levels,
        "policy_version": c.policy_version,
    })


def _json_to_cfg(s: str) -> GridConfig:
    d = json.loads(s)
    return GridConfig(
        instance_id=d["instance_id"], venue=Venue(d["venue"]), market=d["market"],
        lower=Decimal(d["lower"]), upper=Decimal(d["upper"]), levels=int(d["levels"]),
        order_size=Decimal(d["order_size"]), spacing=Spacing(d["spacing"]),
        leverage=Decimal(d["leverage"]), max_levels=int(d["max_levels"]),
        policy_version=d["policy_version"],
    )


class SqliteStore:
    """Implements StorePort. See perpsagent.domain.ports.StorePort."""

    def __init__(self, db_path: str = "perpsagent.db") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL + NORMAL sync: ~10x faster commits than the default rollback journal
        # while staying crash-durable (only the last txns risk loss on OS/power
        # crash, not an app crash) — we keep the per-fill commit (persist BEFORE the
        # paired order). busy_timeout avoids "database is locked" under concurrency.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        try:  # migrate older DBs
            self._conn.execute("ALTER TABLE instances ADD COLUMN regime TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    async def record_fill(self, fill: Fill) -> None:
        await asyncio.to_thread(self._record_fill, fill)

    def _record_fill(self, f: Fill) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fills(instance_id,market,side,price,qty,external_id,ts,level) VALUES(?,?,?,?,?,?,?,?)",
                (f.instance_id, f.market, f.side.value, str(f.price), str(f.qty), f.external_id, f.ts, f.level),
            )
            self._conn.commit()

    async def save_instance(self, cfg: GridConfig, state: str = "RUNNING", regime_json: str | None = None) -> None:
        await asyncio.to_thread(self._save_instance, cfg, state, regime_json)

    def _save_instance(self, c: GridConfig, state: str, regime_json: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO instances(instance_id,venue,market,config,state,created_at,regime) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(instance_id) DO UPDATE SET state=excluded.state, config=excluded.config, "
                "regime=COALESCE(excluded.regime, instances.regime)",
                (c.instance_id, c.venue.value, c.market, _cfg_to_json(c), state, int(time.time()), regime_json),
            )
            self._conn.commit()

    async def load_regime(self, instance_id: str) -> str | None:
        return await asyncio.to_thread(self._load_regime, instance_id)

    def _load_regime(self, instance_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT regime FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
        return row[0] if row and row[0] else None

    async def set_state(self, instance_id: str, state: str) -> None:
        await asyncio.to_thread(self._set_state, instance_id, state)

    def _set_state(self, instance_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE instances SET state=? WHERE instance_id=?", (state, instance_id))
            self._conn.commit()

    async def load_open_instances(self) -> Sequence[GridConfig]:
        return await asyncio.to_thread(self._load_open)

    def _load_open(self) -> list[GridConfig]:
        placeholders = ",".join("?" * len(_OPEN_STATES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT config FROM instances WHERE state IN ({placeholders})", _OPEN_STATES
            ).fetchall()
        return [_json_to_cfg(r[0]) for r in rows]

    async def load_fills(self, instance_id: str) -> list[Fill]:
        return await asyncio.to_thread(self._load_fills, instance_id)

    def _load_fills(self, instance_id: str) -> list[Fill]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT instance_id,market,side,price,qty,external_id,ts,level FROM fills "
                "WHERE instance_id=? ORDER BY id",
                (instance_id,),
            ).fetchall()
        return [
            Fill(instance_id=r[0], market=r[1], side=Side(r[2]), price=Decimal(r[3]),
                 qty=Decimal(r[4]), external_id=r[5], ts=int(r[6]), level=int(r[7]))
            for r in rows
        ]

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
