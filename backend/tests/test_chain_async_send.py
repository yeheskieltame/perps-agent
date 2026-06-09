"""MantleChainClient send routing — commit/settle are confirmed inline, attest/
memory are fire-then-confirm. Constructed offline (chain_id passed → no node call);
the nonce manager is swapped for a recorder, so no network is touched."""
from decimal import Decimal

import pytest
from eth_account import Account

from perpsagent.adapters.chain.client import MantleChainClient
from perpsagent.domain.models import (
    EpisodeOutcome,
    GridConfig,
    MemoryRecord,
    RegimeFingerprint,
    Spacing,
    Venue,
)


def _cfg():
    return GridConfig("i1", Venue.MANTLE_DEX, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                      Decimal("0.01"), Spacing.GEOMETRIC)


def _outcome():
    return EpisodeOutcome("i1", Decimal("1.5"), 0.7, 0.0, 10, "0x" + "ab" * 32, 0.9, False)


class _RecordingNonce:
    def __init__(self):
        self.submitted = 0
        self.confirmed: list[str] = []

    async def submit(self, fn, gas_price=None):
        self.submitted += 1
        return f"0xtx{self.submitted}"

    async def confirm(self, tx_hash, timeout=120):
        self.confirmed.append(tx_hash)


def _client(tmp_path) -> MantleChainClient:
    client = MantleChainClient(
        rpc_url="http://localhost:8545",
        private_key=Account.create().key.hex(),
        ledger_addr="0x" + "11" * 20, memory_addr="0x" + "22" * 20,
        chain_id=5003, detail_path=str(tmp_path / "mirror.json"),
    )
    client._nonce = _RecordingNonce()  # no network
    return client


@pytest.mark.asyncio
async def test_commit_confirmed_inline(tmp_path):
    client = _client(tmp_path)
    tx = await client.commit_strategy("i1", _cfg())
    assert tx == "0xtx1"
    assert client._nonce.confirmed == ["0xtx1"]   # pre-commitment awaited confirmation


@pytest.mark.asyncio
async def test_attest_is_fire_then_confirm(tmp_path):
    client = _client(tmp_path)
    tx = await client.attest("i1", _outcome())
    assert tx == "0xtx1"
    assert "0xtx1" not in client._nonce.confirmed   # returned before confirmation
    await client.drain()                             # background confirm completes
    assert "0xtx1" in client._nonce.confirmed


class _DroppedTxNonce(_RecordingNonce):
    """confirm always times out (tx dropped from the mempool)."""

    def __init__(self):
        super().__init__()
        self.resyncs = 0

    async def confirm(self, tx_hash, timeout=120):
        raise RuntimeError(f"tx dropped: {tx_hash}")

    async def resync(self):
        self.resyncs += 1


@pytest.mark.asyncio
async def test_confirm_failure_resyncs_nonce_on_both_paths(tmp_path):
    """A dropped tx leaves the local nonce counter ahead of the chain; without a
    resync every later tx queues behind a ghost nonce forever."""
    client = _client(tmp_path)
    client._nonce = _DroppedTxNonce()
    with pytest.raises(RuntimeError):                 # confirmed path re-raises…
        await client.commit_strategy("i1", _cfg())
    assert client._nonce.resyncs == 1                 # …after re-seeding the lane
    await client.attest("i1", _outcome())             # fire-then-confirm path
    await client.drain()
    assert client._nonce.resyncs == 2                 # background failure also resyncs


@pytest.mark.asyncio
async def test_write_memory_fire_then_confirm_and_mirrors(tmp_path):
    client = _client(tmp_path)
    record = MemoryRecord(regime=RegimeFingerprint(0.2, 0.0, 0.0001, 0.02, 0.0),
                          config=_cfg(), outcome=_outcome())
    tx = await client.write_memory(record)
    assert tx == "0xtx1" and "0xtx1" not in client._nonce.confirmed
    await client.drain()
    assert client._nonce.confirmed == ["0xtx1"]
