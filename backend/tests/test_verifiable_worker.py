"""The Verifiable Learning Loop, now behind the multi-tenant GridService facade
(Commit 1). A grid launched through the bot's path COMMITs its config hash on-chain
BEFORE any order rests, and on stop ATTESTs the outcome + writes StrategyMemory —
the same trustless loop the CLI runner has. FakeExchange + MemoryChain: no keys,
no network. The UI never sees the chain; it only ever calls create_grid/stop_grid.
"""
from decimal import Decimal

import pytest

from perpsagent.adapters.chain.memory_chain import MemoryChain
from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.app.service import AppService
from perpsagent.domain.models import GridConfig, Spacing, Venue


def _cfg(instance="BTCUSDT-0-abc", market="BTCUSDT"):
    return GridConfig(instance, Venue.FAKE, market, Decimal("99"), Decimal("101"), 10,
                      Decimal("0.001"), Spacing.GEOMETRIC)


def _svc(chain=None):
    return AppService(client_factory=lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                      chain=chain)


@pytest.mark.asyncio
async def test_launch_commits_before_trading_then_attests_and_learns():
    chain = MemoryChain()
    svc = _svc(chain)
    cfg = _cfg()

    iid = await svc.create_grid(7, cfg)
    assert iid in chain._commits                      # COMMIT landed (pre-trade)
    assert svc.proofs(iid)["commit"].startswith("0x")

    await svc.stop_grid(7, iid)
    assert iid in chain._attestations                 # ATTEST the verified outcome
    assert chain._memory                              # LEARN: a StrategyMemory record written
    p = svc.proofs(iid)
    assert "attest" in p and "memory" in p


@pytest.mark.asyncio
async def test_attest_failure_does_not_break_the_close():
    class NonceRace(MemoryChain):
        async def attest(self, instance_id, outcome):
            raise RuntimeError("nonce too low")

    svc = _svc(NonceRace())
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)                        # must not raise
    assert "attest" not in svc.proofs(iid)            # failure recorded by absence


@pytest.mark.asyncio
async def test_no_chain_configured_means_no_proofs():
    svc = _svc(chain=None)
    iid = await svc.create_grid(7, _cfg())
    assert svc.proofs(iid) == {}                       # proofs disabled → unchanged behaviour
    await svc.stop_grid(7, iid)                         # still a clean close


# ---- builder fee (monetization #1): operator bond → treasury, on-chain ----

class FeeChain(MemoryChain):
    """MemoryChain that also exposes a Vault + operator account, recording settles."""

    def __init__(self):
        super().__init__()
        self.vault = object()                          # presence = "vault configured"
        self.acct = type("Acct", (), {"address": "0xOperator"})()
        self.fees: list = []

    async def settle_fee(self, user, asset, amount):
        self.fees.append((user, asset, amount))
        return self._txhash()


def _fee_svc(chain, **kw):
    return AppService(client_factory=lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                      chain=chain, **kw)


@pytest.mark.asyncio
async def test_builder_fee_settles_from_operator_bond_on_close():
    chain = FeeChain()
    svc = _fee_svc(chain, builder_fee=10000, fee_asset="0xUSDC")
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)
    assert chain.fees == [("0xOperator", "0xUSDC", 10000)]   # operator → treasury
    assert "fee" in svc.proofs(iid)


@pytest.mark.asyncio
async def test_builder_fee_off_by_default():
    chain = FeeChain()
    svc = _fee_svc(chain)                               # builder_fee defaults to 0
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)
    assert chain.fees == [] and "fee" not in svc.proofs(iid)


@pytest.mark.asyncio
async def test_builder_fee_revert_is_non_fatal():
    class Broke(FeeChain):
        async def settle_fee(self, user, asset, amount):
            raise RuntimeError("InsufficientBalance")     # no bond deposited

    svc = _fee_svc(Broke(), builder_fee=10000, fee_asset="0xUSDC")
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)                            # must not raise
    assert "fee" not in svc.proofs(iid)


class NativeChain(MemoryChain):
    """MemoryChain that pays a native MNT fee (no Vault, no ERC-20)."""

    def __init__(self):
        super().__init__()
        self.sends: list = []

    async def send_native(self, to, amount):
        self.sends.append((to, amount))
        return self._txhash()


@pytest.mark.asyncio
async def test_builder_fee_native_mnt_default_path():
    chain = NativeChain()                                  # no .vault attr → native path
    svc = _fee_svc(chain, builder_fee=1000, treasury="0xTreasury")  # no fee_asset
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)
    assert chain.sends == [("0xTreasury", 1000)]           # operator → treasury, in MNT
    assert "fee" in svc.proofs(iid)


@pytest.mark.asyncio
async def test_builder_fee_native_needs_a_treasury():
    chain = NativeChain()
    svc = _fee_svc(chain, builder_fee=1000)                # treasury unset → skip
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)
    assert chain.sends == [] and "fee" not in svc.proofs(iid)
