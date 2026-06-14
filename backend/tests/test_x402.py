"""x402 gateway: challenge shape, encoding, and REAL EIP-3009/EIP-712 signature
verification (sign with a throwaway key, recover, replay/expiry/tamper guards)."""
import os
import time

import pytest
from eth_account import Account

from perpsagent.adapters.payments.x402 import (
    X402Gateway, b64decode_json, b64encode_json, split_signature,
)

PAY_TO = "0x" + "aa" * 20
ASSET = "0x" + "bb" * 20


def gw(**kw):
    return X402Gateway(pay_to=PAY_TO, asset=ASSET, **kw)


def make_payment(g, acct, value="10000", to=PAY_TO, before_offset=600, nonce=None):
    auth = {
        "from": acct.address, "to": to, "value": value, "validAfter": "0",
        "validBefore": str(int(time.time()) + before_offset),
        "nonce": nonce or "0x" + os.urandom(32).hex(),
    }
    signed = Account.sign_typed_data(acct.key, full_message=g._typed_data(auth))
    sig = signed.signature.hex()
    return {
        "x402Version": 1, "scheme": "exact", "network": g.network,
        "payload": {"signature": sig if sig.startswith("0x") else "0x" + sig, "authorization": auth},
    }


def test_challenge_shape():
    ch = gw().challenge("https://api/x")
    assert ch["x402Version"] == 1
    a = ch["accepts"][0]
    assert a["scheme"] == "exact" and a["payTo"] == PAY_TO and a["asset"] == ASSET
    assert a["maxAmountRequired"] == "10000" and a["network"] == "mantle-sepolia"


def test_b64_roundtrip_and_split_signature():
    obj = {"a": 1, "b": "x"}
    assert b64decode_json(b64encode_json(obj)) == obj
    v, r, s = split_signature("0x" + "11" * 32 + "22" * 32 + "1b")
    assert v == 27 and len(r) == 32 and len(s) == 32


def test_verify_valid_then_replay_blocked():
    g = gw()
    acct = Account.create()
    p = make_payment(g, acct)
    req = g.requirements("https://api/x")
    ok, reason = g.verify_local(p, req)
    assert ok, reason
    ok2, reason2 = g.verify_local(p, req)
    assert not ok2 and "replay" in reason2


def test_verify_rejections():
    g = gw()
    acct = Account.create()
    req = g.requirements("https://api/x")
    assert g.verify_local(make_payment(g, acct, to="0x" + "cc" * 20), req)[0] is False  # wrong payTo
    assert g.verify_local(make_payment(g, acct, before_offset=-10), req)[0] is False    # expired
    assert g.verify_local(make_payment(g, acct, value="1"), req)[0] is False            # insufficient
    tampered = make_payment(g, acct)
    tampered["payload"]["authorization"]["value"] = "999999999"                          # sig mismatch
    assert g.verify_local(tampered, req)[0] is False


@pytest.mark.asyncio
async def test_settle_deferred_mode():
    g = gw()
    acct = Account.create()
    res = await g.settle(make_payment(g, acct), g.requirements("r"))
    assert res["success"] and res["mode"] == "deferred"
    assert res["payer"].lower() == acct.address.lower()


# ---- native MNT scheme: pay the gas token directly, verified on-chain ----

class _NativeGW(X402Gateway):
    """Native gateway with a stubbed on-chain tx lookup (no node needed)."""

    def __init__(self, **kw):
        super().__init__(pay_to=PAY_TO, asset="MNT", native=True, rpc_url="http://stub", **kw)
        self.onchain: dict[str, tuple] = {}  # txHash -> (to, value, status)

    def _fetch_tx(self, tx_hash):
        return self.onchain.get(tx_hash, (None, 0, 0))


def _native_payment(tx_hash="0x" + "11" * 32):
    return {"x402Version": 1, "scheme": "mnt-native", "network": "mantle-sepolia",
            "payload": {"txHash": tx_hash, "from": "0x" + "99" * 20}}


def test_native_challenge_advertises_mnt():
    a = _NativeGW().challenge("https://api/x")["accepts"][0]
    assert a["scheme"] == "mnt-native" and a["asset"] == "MNT"
    assert a["extra"] == {"symbol": "MNT", "decimals": 18}


@pytest.mark.asyncio
async def test_native_verify_accepts_confirmed_payment_then_blocks_replay():
    g = _NativeGW()
    req = g.requirements("r")  # maxAmountRequired = default 10000
    g.onchain["0x" + "11" * 32] = (PAY_TO, 50000, 1)  # to=payTo, enough, confirmed
    ok, reason = await g.verify(_native_payment(), req)
    assert ok and reason == "ok"
    ok2, reason2 = await g.verify(_native_payment(), req)   # same tx again
    assert not ok2 and "replay" in reason2
    res = await g.settle(_native_payment(), req)
    assert res["mode"] == "native" and res["transaction"] == "0x" + "11" * 32


@pytest.mark.asyncio
async def test_native_verify_rejects_bad_payments():
    g = _NativeGW()
    req = g.requirements("r")
    g.onchain = {
        "0xunconfirmed": (PAY_TO, 50000, 0),     # reverted/pending
        "0xwrongto": ("0x" + "cc" * 20, 50000, 1),
        "0xtoolow": (PAY_TO, 1, 1),
    }
    assert (await g.verify(_native_payment("0xunconfirmed"), req))[0] is False
    assert (await g.verify(_native_payment("0xwrongto"), req))[0] is False
    assert (await g.verify(_native_payment("0xtoolow"), req))[0] is False
    assert (await g.verify(_native_payment("0xmissing"), req))[0] is False  # unknown tx
