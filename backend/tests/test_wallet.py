"""Per-user managed MNT wallet (Opsi A): mint-on-first-use, Fernet-sealed, and the
builder fee debited from the USER's wallet. On-chain calls are stubbed (no node)."""
import pytest

from perpsagent.adapters.exchanges.fake import FakeExchange
from perpsagent.adapters.store.credentials import CredentialCodec
from perpsagent.adapters.store.wallets import WalletAdmin
from perpsagent.app.service import AppService
from perpsagent.domain.models import GridConfig, Spacing, Venue
from decimal import Decimal


class FakeWalletStore:
    def __init__(self):
        self.rows: dict[int, bytes] = {}

    async def put_wallet(self, user_id, ciphertext):
        self.rows[user_id] = ciphertext

    async def get_wallet(self, user_id):
        return self.rows.get(user_id)


def _codec():
    return CredentialCodec(CredentialCodec.generate_key())


@pytest.mark.asyncio
async def test_wallet_minted_once_and_persisted():
    admin = WalletAdmin(FakeWalletStore(), _codec())
    addr = await admin.get_or_create(42)
    assert addr.startswith("0x") and len(addr) == 42
    assert await admin.get_or_create(42) == addr      # idempotent — same wallet
    assert await admin.address(42) == addr
    # a different user gets a different wallet
    assert await admin.get_or_create(43) != addr


@pytest.mark.asyncio
async def test_wallet_key_is_encrypted_at_rest():
    store = FakeWalletStore()
    admin = WalletAdmin(store, _codec())
    addr = await admin.get_or_create(42)
    blob = store.rows[42]
    assert addr.encode() not in blob              # address (hence key) not in cleartext
    assert b"private_key" not in blob


@pytest.mark.asyncio
async def test_balance_zero_without_rpc():
    admin = WalletAdmin(FakeWalletStore(), _codec())  # no rpc_url
    await admin.get_or_create(42)
    assert await admin.balance(42) == 0


def _cfg(instance="BTCUSDT-0-w"):
    return GridConfig(instance, Venue.FAKE, "BTCUSDT", Decimal("99"), Decimal("101"), 10,
                      Decimal("0.001"), Spacing.GEOMETRIC)


@pytest.mark.asyncio
async def test_builder_fee_debits_the_user_wallet():
    calls = []

    async def fee_payer(user_id, to, amount):
        calls.append((user_id, to, amount))
        return "0xwalletfee"

    svc = AppService(client_factory=lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                     builder_fee=1000, treasury="0xTreasury", fee_payer=fee_payer)
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)
    assert calls == [(7, "0xTreasury", 1000)]        # the USER (id 7) pays, to treasury
    assert svc.proofs(iid)["fee"] == "0xwalletfee"


@pytest.mark.asyncio
async def test_user_fee_failure_is_non_fatal():
    async def broke(user_id, to, amount):
        raise RuntimeError("insufficient funds — top up your wallet")

    svc = AppService(client_factory=lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"}),
                     builder_fee=1000, treasury="0xTreasury", fee_payer=broke)
    iid = await svc.create_grid(7, _cfg())
    await svc.stop_grid(7, iid)                       # must not raise
    assert "fee" not in svc.proofs(iid)


# ---- worker GET /v1/wallet ----

class _FakeWalletAdmin:
    async def get_or_create(self, user_id):
        return "0x" + f"{user_id:040x}"

    async def balance(self, user_id):
        return 1234


async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _worker_svc():
    return AppService(client_factory=lambda _uid: FakeExchange({"mid": "100", "tick": "0.1"}))


@pytest.mark.asyncio
async def test_worker_wallet_endpoint_returns_address_and_balance():
    from perpsagent.app.worker import build_worker_app

    c = await _client(build_worker_app(_worker_svc(), "0", wallet=_FakeWalletAdmin()))
    try:
        r = await c.get("/v1/wallet", headers={"X-User-Id": "7"})
        assert r.status == 200
        body = await r.json()
        assert body["address"].startswith("0x") and body["currency"] == "MNT"
        assert body["balance"] == "1234" and "faucet" in body
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_worker_wallet_503_when_not_configured():
    from perpsagent.app.worker import build_worker_app

    c = await _client(build_worker_app(_worker_svc(), "0"))  # no wallet admin
    try:
        assert (await c.get("/v1/wallet", headers={"X-User-Id": "7"})).status == 503
    finally:
        await c.close()
