"""NonceManager — race-free nonce allocation + non-blocking submission (fakes only)."""
import asyncio

import pytest

from perpsagent.adapters.chain.nonce import NonceManager


class _Signed:
    def __init__(self, tx):
        self.raw_transaction = b"raw-%d" % tx["nonce"]


class _FakeAcct:
    def __init__(self):
        self.address = "0xacct"
        self.signed_nonces: list[int] = []

    def sign_transaction(self, tx):
        self.signed_nonces.append(tx["nonce"])
        return _Signed(tx)


class _FakeEth:
    def __init__(self, start_nonce=0):
        self.start_nonce = start_nonce
        self.gas_price = 1_000_000_000
        self.count_calls = 0
        self.wait_calls = 0
        self.receipts: dict[str, int] = {}

    def get_transaction_count(self, addr, block):
        self.count_calls += 1
        return self.start_nonce

    def send_raw_transaction(self, raw):
        return "0x" + (b"h" + raw).hex()

    def wait_for_transaction_receipt(self, tx_hash, timeout=120):
        self.wait_calls += 1
        return {"status": self.receipts.get(tx_hash, 1)}


class _FakeW3:
    def __init__(self, eth):
        self.eth = eth


class _FakeFn:
    def build_transaction(self, params):
        return params  # echo: includes the allocated nonce


@pytest.mark.asyncio
async def test_seeds_once_and_increments():
    eth, acct = _FakeEth(start_nonce=5), _FakeAcct()
    nm = NonceManager(_FakeW3(eth), acct, 5003)
    h0, h1, h2 = await nm.submit(_FakeFn()), await nm.submit(_FakeFn()), await nm.submit(_FakeFn())
    assert acct.signed_nonces == [5, 6, 7]   # strictly increasing from the seed
    assert eth.count_calls == 1              # seeded ONCE, not per send
    assert len({h0, h1, h2}) == 3            # distinct hashes


@pytest.mark.asyncio
async def test_concurrent_submits_never_collide():
    eth, acct = _FakeEth(start_nonce=0), _FakeAcct()
    nm = NonceManager(_FakeW3(eth), acct, 1)
    await asyncio.gather(*[nm.submit(_FakeFn()) for _ in range(20)])
    assert sorted(acct.signed_nonces) == list(range(20))  # 20 unique nonces, no dup
    assert eth.count_calls == 1


@pytest.mark.asyncio
async def test_submit_does_not_wait_for_receipt():
    eth = _FakeEth()
    nm = NonceManager(_FakeW3(eth), _FakeAcct(), 1)
    h = await nm.submit(_FakeFn())
    assert eth.wait_calls == 0     # submission is non-blocking
    await nm.confirm(h)
    assert eth.wait_calls == 1     # confirmation is a separate step


@pytest.mark.asyncio
async def test_confirm_raises_on_revert():
    eth = _FakeEth()
    nm = NonceManager(_FakeW3(eth), _FakeAcct(), 1)
    h = await nm.submit(_FakeFn())
    eth.receipts[h] = 0            # reverted
    with pytest.raises(RuntimeError):
        await nm.confirm(h)


@pytest.mark.asyncio
async def test_resync_reseeds_from_chain():
    eth, acct = _FakeEth(start_nonce=10), _FakeAcct()
    nm = NonceManager(_FakeW3(eth), acct, 1)
    await nm.submit(_FakeFn())     # nonce 10
    eth.start_nonce = 100          # chain advanced out-of-band
    await nm.resync()
    await nm.submit(_FakeFn())     # nonce 100
    assert acct.signed_nonces == [10, 100]
    assert eth.count_calls == 2


@pytest.mark.asyncio
async def test_stale_nonce_reseeds_and_retries():
    """Live 2026-06-11: two runner processes shared one key; the laggard's cached
    nonce went stale and its attest died with 'nonce too low'. A stale-nonce
    broadcast error must re-seed from the chain and retry once."""

    class SharedKeyEth(_FakeEth):
        def get_transaction_count(self, addr, block):
            self.count_calls += 1
            return 74 if self.count_calls == 1 else 80  # stale seed, then fresh

        def send_raw_transaction(self, raw):
            # the chain is at 80 (another process advanced it); reject anything lower
            if int(raw.decode().split("-")[1]) < 80:
                raise ValueError(
                    "{'code': -32000, 'message': \"failed to forward tx to sequencer, "
                    "err: 'nonce too low: next nonce 80, tx nonce 74'\"}")
            return super().send_raw_transaction(raw)

    eth, acct = SharedKeyEth(), _FakeAcct()
    nm = NonceManager(_FakeW3(eth), acct, 5003)
    h = await nm.submit(_FakeFn())             # first try at 74 fails -> reseed -> 80
    assert h.startswith("0x")
    assert acct.signed_nonces == [74, 80]      # one retry, at the re-seeded nonce
    assert eth.count_calls == 2                # seeded twice: initial + re-seed


@pytest.mark.asyncio
async def test_non_nonce_errors_still_raise():
    class Refuses(_FakeEth):
        def send_raw_transaction(self, raw):
            raise ValueError("insufficient funds for gas")

    nm = NonceManager(_FakeW3(Refuses()), _FakeAcct(), 5003)
    with pytest.raises(ValueError, match="insufficient funds"):
        await nm.submit(_FakeFn())
