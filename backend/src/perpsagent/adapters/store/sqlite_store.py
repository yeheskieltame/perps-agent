"""SQLite persistence — implements StorePort (+ recovery helpers).

Durably records grid instances and fills so the engine can recover realized
state after a restart (production-grade; mirrors the deltaperps recovery
principle). Synchronous sqlite3 wrapped in asyncio.to_thread so the async engine
never blocks. Upgrade path: Postgres behind the same StorePort.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from decimal import Decimal
from typing import Sequence

from ...domain.models import Fill, GridConfig, Side
from .serde import cfg_to_json, json_to_cfg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    venue TEXT, market TEXT, config TEXT, state TEXT, created_at INTEGER, regime TEXT,
    user_id INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT, market TEXT, side TEXT, price TEXT, qty TEXT,
    external_id TEXT, ts INTEGER, level INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fills_instance ON fills(instance_id);
CREATE TABLE IF NOT EXISTS credentials (
    user_id INTEGER PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    settings TEXT NOT NULL,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS wallets (
    user_id INTEGER PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, instance_id TEXT, market TEXT,
    realized_pnl TEXT, fill_count INTEGER, winrate REAL, closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id);
CREATE TABLE IF NOT EXISTS grid_names (
    instance_id TEXT PRIMARY KEY, user_id INTEGER, name TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS spent_payments (
    key TEXT PRIMARY KEY, ts INTEGER
);
"""
_OPEN_STATES = ("INITIALIZING", "RUNNING", "REBALANCING")


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
        for col in ("regime TEXT", "user_id INTEGER"):  # migrate older DBs
            try:
                self._conn.execute(f"ALTER TABLE instances ADD COLUMN {col}")
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
                (c.instance_id, c.venue.value, c.market, cfg_to_json(c), state, int(time.time()), regime_json),
            )
            self._conn.commit()

    async def set_owner(self, instance_id: str, user_id: int) -> None:
        await asyncio.to_thread(self._set_owner, instance_id, user_id)

    def _set_owner(self, instance_id: str, user_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE instances SET user_id=? WHERE instance_id=?", (user_id, instance_id))
            self._conn.commit()

    async def load_open_with_owner(self) -> list[tuple[int, GridConfig]]:
        """Open instances paired with their owner — drives per-user recovery."""
        return await asyncio.to_thread(self._load_open_with_owner)

    def _load_open_with_owner(self) -> list[tuple[int, GridConfig]]:
        placeholders = ",".join("?" * len(_OPEN_STATES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT user_id, config FROM instances WHERE state IN ({placeholders}) AND user_id IS NOT NULL",
                _OPEN_STATES,
            ).fetchall()
        return [(int(r[0]), json_to_cfg(r[1])) for r in rows]

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
        return [json_to_cfg(r[0]) for r in rows]

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

    # ---- credentials (CredentialStorePort) ----

    async def put_credentials(self, user_id: int, ciphertext: bytes) -> None:
        await asyncio.to_thread(self._put_credentials, user_id, ciphertext)

    def _put_credentials(self, user_id: int, ciphertext: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO credentials(user_id,ciphertext,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET ciphertext=excluded.ciphertext, updated_at=excluded.updated_at",
                (user_id, ciphertext, int(time.time())),
            )
            self._conn.commit()

    async def get_credentials(self, user_id: int) -> bytes | None:
        return await asyncio.to_thread(self._get_credentials, user_id)

    def _get_credentials(self, user_id: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM credentials WHERE user_id=?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    async def delete_credentials(self, user_id: int) -> None:
        await asyncio.to_thread(self._delete_credentials, user_id)

    def _delete_credentials(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM credentials WHERE user_id=?", (user_id,))
            self._conn.commit()

    # ---- managed MNT wallets (WalletStorePort) ----

    async def put_wallet(self, user_id: int, ciphertext: bytes) -> None:
        await asyncio.to_thread(self._put_wallet, user_id, ciphertext)

    def _put_wallet(self, user_id: int, ciphertext: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO wallets(user_id,ciphertext,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET ciphertext=excluded.ciphertext, updated_at=excluded.updated_at",
                (user_id, ciphertext, int(time.time())),
            )
            self._conn.commit()

    async def get_wallet(self, user_id: int) -> bytes | None:
        return await asyncio.to_thread(self._get_wallet, user_id)

    def _get_wallet(self, user_id: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM wallets WHERE user_id=?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    # ---- closed-episode history ----

    async def record_episode(self, user_id: int, instance_id: str, market: str,
                             realized_pnl: str, fill_count: int, winrate: float) -> None:
        await asyncio.to_thread(self._record_episode, user_id, instance_id, market,
                                realized_pnl, fill_count, winrate)

    def _record_episode(self, user_id, instance_id, market, realized_pnl, fill_count, winrate) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO episodes(user_id,instance_id,market,realized_pnl,fill_count,winrate,closed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, instance_id, market, str(realized_pnl), int(fill_count),
                 float(winrate), int(time.time())),
            )
            self._conn.commit()

    async def load_episodes(self, user_id: int, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._load_episodes, user_id, limit)

    def _load_episodes(self, user_id: int, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT instance_id,market,realized_pnl,fill_count,winrate,closed_at FROM episodes "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit),
            ).fetchall()
        return [{"instance_id": r[0], "market": r[1], "realized_pnl": r[2],
                 "fill_count": r[3], "winrate": r[4], "closed_at": r[5]} for r in rows]

    # ---- user-assigned grid names ----

    async def set_grid_name(self, user_id: int, instance_id: str, name: str) -> None:
        await asyncio.to_thread(self._set_grid_name, user_id, instance_id, name)

    def _set_grid_name(self, user_id: int, instance_id: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO grid_names(instance_id,user_id,name,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(instance_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (instance_id, user_id, name, int(time.time())),
            )
            self._conn.commit()

    async def load_grid_names(self, user_id: int) -> dict[str, str]:
        return await asyncio.to_thread(self._load_grid_names, user_id)

    def _load_grid_names(self, user_id: int) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT instance_id,name FROM grid_names WHERE user_id=?", (user_id,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ---- per-user strategy settings (JSON of validated knobs — app/prefs.py) ----

    async def put_settings(self, user_id: int, settings_json: str) -> None:
        await asyncio.to_thread(self._put_settings, user_id, settings_json)

    def _put_settings(self, user_id: int, settings_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_settings(user_id,settings,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings, updated_at=excluded.updated_at",
                (user_id, settings_json, int(time.time())),
            )
            self._conn.commit()

    async def get_settings(self, user_id: int) -> str | None:
        return await asyncio.to_thread(self._get_settings, user_id)

    def _get_settings(self, user_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT settings FROM user_settings WHERE user_id=?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    async def delete_settings(self, user_id: int) -> None:
        await asyncio.to_thread(self._delete_settings, user_id)

    def _delete_settings(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
            self._conn.commit()

    # ---- x402 spent-payment replay guard ----

    async def claim_nonce(self, key: str) -> bool:
        """Atomically record a spent payment nonce/txHash. True if newly claimed,
        False if already seen (a replay). Survives restarts — unlike an in-process set."""
        return await asyncio.to_thread(self._claim_nonce, key)

    def _claim_nonce(self, key: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("INSERT INTO spent_payments(key,ts) VALUES(?,?)", (key, int(time.time())))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
