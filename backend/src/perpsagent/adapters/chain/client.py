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
            cfg.order_size, cfg.leverage, cfg.policy_version,
        )
    )
    return keccak(text=canonical)


def _bps(x: float) -> int:
    return int(round(x * 10_000))


def _clip_i32(v: int) -> int:
    return max(_I32_MIN, min(_I32_MAX, v))


def _b32(hexstr: str) -> bytes:
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return bytes.fromhex(h).rjust(32, b"\x00")[:32]


def _raw(signed) -> bytes:
    return getattr(signed, "raw_transaction", None) or signed.rawTransaction


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
                "policy_version": cfg.policy_version,
            },
            "regime": regime.__dict__,
        }
        if self.path:
            self.path.write_text(json.dumps(self._d))

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
            policy_version=c["policy_version"],
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

    @staticmethod
    def _instance_b32(instance_id: str) -> bytes:
        return keccak(text=instance_id)

    # ---- writes ----

    async def commit_strategy(self, instance_id: str, config: GridConfig) -> str:
        return await self._send(self.ledger.functions.commitStrategy(self._instance_b32(instance_id), config_hash(config)))

    async def attest(self, instance_id: str, outcome: EpisodeOutcome) -> str:
        return await self._send(
            self.ledger.functions.attest(
                self._instance_b32(instance_id),
                int(outcome.realized_pnl * _PNL_SCALE),
                _bps(outcome.winrate),
                _clip_i32(_bps(outcome.risk_adjusted)),
                _b32(outcome.fills_merkle_root or "0x" + "00" * 32),
            )
        )

    async def write_memory(self, record: MemoryRecord) -> str:
        ch = config_hash(record.config)
        self.mirror.put(ch.hex(), record.config, record.regime)
        return await self._send(
            self.memory.functions.write(
                _b32(regime_key(record.regime)),
                ch,
                int(record.outcome.realized_pnl * _PNL_SCALE),
                _bps(record.outcome.winrate),
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
        return await self._send(
            self.vault.functions.settleFee(Web3.to_checksum_address(user), Web3.to_checksum_address(asset), int(amount))
        )

    async def vault_balance(self, user: str, asset: str) -> int:
        assert self.vault is not None
        return await asyncio.to_thread(
            lambda: self.vault.functions.balanceOf(Web3.to_checksum_address(user), Web3.to_checksum_address(asset)).call()
        )

    # ---- tx plumbing ----

    async def _send(self, fn) -> str:
        return await asyncio.to_thread(self._send_sync, fn)

    def _send_sync(self, fn) -> str:
        tx = fn.build_transaction(
            {
                "from": self.acct.address,
                "nonce": self.w3.eth.get_transaction_count(self.acct.address, "pending"),
                "chainId": self.chain_id,
                "gasPrice": self.w3.eth.gas_price,
            }
        )
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(_raw(signed))
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if rcpt["status"] != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()
