"""MantleChainClient — ChainPort over web3.py against the deployed StrategyLedger,
StrategyMemory and Vault (Perps Agent L1/L2).

This is the layer that makes Mantle the agent's *verifiable brain*:
- commit_strategy / attest / write_memory are real signed transactions;
- recall reads on-chain records back for analysis + the policy.

On-chain we store compact, verifiable records (regimeKey, configHash, outcome
metrics). The full GridConfig + RegimeFingerprint live in an off-chain detail
mirror keyed by configHash so recall can reconstruct usable params. In production
the mirror is IPFS/DB; here it is an optional JSON file. Records authored by other
agents (no local detail) still count on-chain but are not param-reconstructable
until the IPFS detail-store lands (roadmap).
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from eth_account import Account
from eth_utils import keccak
from web3 import Web3

from ...domain.models import (
    EpisodeOutcome,
    GridConfig,
    MemoryQuery,
    MemoryRecord,
    RegimeFingerprint,
    Spacing,
    Venue,
)
from ...domain.regime import regime_key
from .nonce import NonceManager

_ABI_DIR = Path(__file__).parent / "abi"
_PNL_SCALE = 10**6  # realizedPnl stored as fixed-point with 6 decimals on-chain
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1


def _abi(name: str) -> list:
    return json.loads((_ABI_DIR / f"{name}.json").read_text())


def config_hash(cfg: GridConfig) -> bytes:
    """Deterministic keccak256 over the canonical config — the pre-commitment."""
    canonical = "|".join(
        str(x)
        for x in (
            cfg.market, cfg.lower, cfg.upper, cfg.levels, cfg.spacing.value,
            cfg.order_size, cfg.leverage, cfg.bias, cfg.policy_version,
        )
    )
    return keccak(text=canonical)


def _bps(x: float) -> int:
    return int(round(x * 10_000))


def _winrate_bps(winrate: float) -> int:
    """Winrate → bps, clamped to [0, 10_000]. The on-chain winrateBps is uint32 and
    the ledger reverts above 10_000, so a garbage/out-of-range winrate must degrade,
    not brick the (fire-then-confirm) attest tx on the close path."""
    return _bps(max(0.0, min(1.0, winrate)))


def _clip_i32(v: int) -> int:
    return max(_I32_MIN, min(_I32_MAX, v))


def _b32(hexstr: str) -> bytes:
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return bytes.fromhex(h).rjust(32, b"\x00")[:32]


class _NativeTransfer:
    """Adapts a plain MNT transfer to the NonceManager's `fn.build_transaction(params)`
    interface, so native sends reuse the same race-free nonce lane as contract calls."""

    def __init__(self, to: str, value: int) -> None:
        self._to, self._value = to, value

    def build_transaction(self, params: dict) -> dict:
        return {**params, "to": self._to, "value": self._value, "gas": 21000}


class _DetailMirror:
    """configHash(hex) -> (GridConfig, RegimeFingerprint). Optional JSON persistence."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else None
        self._d: dict[str, dict] = {}
        if self.path and self.path.exists():
            self._d = json.loads(self.path.read_text())

    def put(self, key_hex: str, cfg: GridConfig, regime: RegimeFingerprint) -> None:
        self._d[key_hex] = {
            "cfg": {
                "instance_id": cfg.instance_id, "venue": cfg.venue.value, "market": cfg.market,
                "lower": str(cfg.lower), "upper": str(cfg.upper), "levels": cfg.levels,
                "order_size": str(cfg.order_size), "spacing": cfg.spacing.value,
                "leverage": str(cfg.leverage), "max_levels": cfg.max_levels,
                "policy_version": cfg.policy_version, "bias": cfg.bias,
            },
            "regime": regime.__dict__,
        }
        if self.path:
            # Atomic write: a crash (or a concurrent reader) never sees a half-written
            # file — write a temp then rename over the target on the same filesystem.
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._d))
            tmp.replace(self.path)

    def get(self, key_hex: str) -> tuple[GridConfig, RegimeFingerprint] | None:
        e = self._d.get(key_hex)
        if e is None:
            return None
        c = e["cfg"]
        cfg = GridConfig(
            instance_id=c["instance_id"], venue=Venue(c["venue"]), market=c["market"],
            lower=Decimal(c["lower"]), upper=Decimal(c["upper"]), levels=int(c["levels"]),
            order_size=Decimal(c["order_size"]), spacing=Spacing(c["spacing"]),
            leverage=Decimal(c["leverage"]), max_levels=int(c["max_levels"]),
            policy_version=c["policy_version"], bias=int(c.get("bias", 0)),
        )
        return cfg, RegimeFingerprint(**e["regime"])


class MantleChainClient:
    """Implements ChainPort. See perpsagent.domain.ports.ChainPort."""

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        ledger_addr: str,
        memory_addr: str,
        vault_addr: str | None = None,
        chain_id: int | None = None,
        detail_path: str | None = None,
    ) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.acct = Account.from_key(private_key)
        self.chain_id = chain_id or self.w3.eth.chain_id
        self.ledger = self.w3.eth.contract(address=Web3.to_checksum_address(ledger_addr), abi=_abi("StrategyLedger"))
        self.memory = self.w3.eth.contract(address=Web3.to_checksum_address(memory_addr), abi=_abi("StrategyMemory"))
        self.vault = (
            self.w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=_abi("Vault"))
            if vault_addr else None
        )
        self.mirror = _DetailMirror(detail_path)
        # One signer = one nonce lane: serialized, race-free, non-blocking submission.
        self._nonce = NonceManager(self.w3, self.acct, self.chain_id)
        self._pending: set[asyncio.Task] = set()

    @staticmethod
    def _instance_b32(instance_id: str) -> bytes:
        return keccak(text=instance_id)

    # ---- writes ----

    async def commit_strategy(self, instance_id: str, config: GridConfig) -> str:
        # Pre-commitment: must be on-chain BEFORE trading → confirmed (awaited).
        return await self._send_confirmed(
            self.ledger.functions.commitStrategy(self._instance_b32(instance_id), config_hash(config))
        )

    async def attest(self, instance_id: str, outcome: EpisodeOutcome) -> str:
        # Post-trade write → fire-then-confirm (don't block the close path on a block).
        return await self._send_async(
            self.ledger.functions.attest(
                self._instance_b32(instance_id),
                int(outcome.realized_pnl * _PNL_SCALE),
                _winrate_bps(outcome.winrate),
                _clip_i32(_bps(outcome.risk_adjusted)),
                _b32(outcome.fills_merkle_root or "0x" + "00" * 32),
            )
        )

    async def write_memory(self, record: MemoryRecord) -> str:
        ch = config_hash(record.config)
        self.mirror.put(ch.hex(), record.config, record.regime)
        return await self._send_async(
            self.memory.functions.write(
                _b32(regime_key(record.regime)),
                ch,
                int(record.outcome.realized_pnl * _PNL_SCALE),
                _winrate_bps(record.outcome.winrate),
                _clip_i32(_bps(record.outcome.risk_adjusted)),
                bool(record.outcome.is_backtest),
            )
        )

    # ---- read ----

    async def recall(self, query: MemoryQuery) -> Sequence[MemoryRecord]:
        return await asyncio.to_thread(self._recall_sync, _b32(regime_key(query.regime)), query.k)

    def _recall_sync(self, regime_key_b32: bytes, k: int) -> list[MemoryRecord]:
        count = self.memory.functions.countByRegime(regime_key_b32).call()
        if count == 0:
            return []
        rows = self.memory.functions.getByRegime(regime_key_b32, 0, min(count, 200)).call()
        out: list[MemoryRecord] = []
        for row in rows:
            # Record(regimeKey, configHash, realizedPnl, winrateBps, riskAdjBps, isBacktest, agent, ts)
            ch_hex = bytes(row[1]).hex()
            detail = self.mirror.get(ch_hex)
            if detail is None:
                continue
            cfg, regime = detail
            outcome = EpisodeOutcome(
                instance_id="",
                realized_pnl=Decimal(row[2]) / _PNL_SCALE,
                winrate=row[3] / 10_000,
                max_adverse_excursion=0.0,
                fill_count=0,
                fills_merkle_root="",
                risk_adjusted=row[4] / 10_000,
                is_backtest=bool(row[5]),
            )
            out.append(MemoryRecord(regime=regime, config=cfg, outcome=outcome))
        out.sort(key=lambda r: r.outcome.risk_adjusted, reverse=True)
        return out[:k]

    # ---- vault convenience (builder-fee settlement) ----

    async def settle_fee(self, user: str, asset: str, amount: int) -> str:
        assert self.vault is not None, "vault address not configured"
        return await self._send_confirmed(  # money movement → confirmed
            self.vault.functions.settleFee(Web3.to_checksum_address(user), Web3.to_checksum_address(asset), int(amount))
        )

    async def vault_balance(self, user: str, asset: str) -> int:
        assert self.vault is not None
        return await asyncio.to_thread(
            lambda: self.vault.functions.balanceOf(Web3.to_checksum_address(user), Web3.to_checksum_address(asset)).call()
        )

    async def send_native(self, to: str, amount: int) -> str:
        """Send `amount` wei of native MNT to `to` (e.g. a builder fee to the
        treasury) — confirmed, since it moves money. Reuses the signer's nonce lane."""
        return await self._send_confirmed(_NativeTransfer(Web3.to_checksum_address(to), int(amount)))

    # ---- tx plumbing (nonce-managed; see adapters/chain/nonce.py) ----

    async def _send_confirmed(self, fn) -> str:
        """Submit AND wait for the receipt — for txs that must be on-chain before we
        proceed (pre-commitment, fee settlement)."""
        tx_hash = await self._nonce.submit(fn)
        try:
            await self._nonce.confirm(tx_hash)
        except Exception:
            await self._resync_nonce()  # a dropped tx must not strand the nonce lane
            raise
        return tx_hash

    async def _send_async(self, fn) -> str:
        """Submit and return the hash immediately; confirm in the background
        (fire-then-confirm). For post-trade writes (attest / memory) that must not
        block the close path on a Mantle block — a revert is logged, not raised."""
        tx_hash = await self._nonce.submit(fn)
        task = asyncio.create_task(self._confirm_and_log(tx_hash))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return tx_hash

    async def _confirm_and_log(self, tx_hash: str) -> None:
        try:
            await self._nonce.confirm(tx_hash)
        except Exception as e:  # noqa: BLE001 — background confirm must never crash the loop
            print(f"  ! on-chain confirm failed {tx_hash}: {e}")
            await self._resync_nonce()

    async def _resync_nonce(self) -> None:
        """Re-seed the nonce after a failed/timed-out confirm: a tx dropped from
        the mempool leaves the local counter ahead of the chain, which would
        queue every later tx behind a ghost nonce forever."""
        try:
            await self._nonce.resync()
        except Exception as e:  # noqa: BLE001 — best-effort; next submit may still work
            print(f"  ! nonce resync failed: {e}")

    async def drain(self) -> None:
        """Await all in-flight background confirmations (call on graceful shutdown)."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
