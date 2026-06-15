"""x402 pay-per-call demo CLIENT — proves native-MNT settlement end to end.

  cd backend && .venv/bin/python scripts/x402_pay.py [URL]

Flow (the buyer side of docs.perpsagent.xyz):
  1. GET the alpha endpoint            -> HTTP 402 + PaymentRequirements (payTo, amount, MNT)
  2. pay that amount of native MNT to payTo on Mantle (a real tx)
  3. retry with header  X-PAYMENT: base64({scheme:"mnt-native", payload:{txHash, from}})
  4. server verifies the transfer on-chain  -> HTTP 200 + the verified-alpha JSON

Reads the payer key + RPC from backend/.env (PERPSAGENT_MANTLE_PRIVATE_KEY,
PERPSAGENT_MANTLE_RPC, PERPSAGENT_X402_CHAIN_ID). The payer may be the operator
wallet itself; the server only checks to==payTo, value>=price, and confirmation.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request

from eth_account import Account
from web3 import Web3

from perpsagent.config import Settings


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _get(url: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> None:
    s = Settings()
    url = sys.argv[1] if len(sys.argv) > 1 else \
        f"http://127.0.0.1:{s.alpha_port}/v1/alpha/recall/BTCUSDT"
    if not s.mantle_private_key or not s.mantle_rpc:
        raise SystemExit("set PERPSAGENT_MANTLE_PRIVATE_KEY + PERPSAGENT_MANTLE_RPC in backend/.env")

    # 1. challenge
    status, body = _get(url)
    if status != 402:
        raise SystemExit(f"expected 402, got {status}: {body}")
    req = body["accepts"][0]
    if req.get("scheme") != "mnt-native":
        raise SystemExit(f"this client only handles mnt-native, got scheme={req.get('scheme')!r}")
    amount = int(req["maxAmountRequired"])
    pay_to = req["payTo"]
    print(f"402 → pay {amount} wei MNT ({amount/1e18:.6f} MNT) to {pay_to} on {req['network']}")

    # 2. pay native MNT
    w3 = Web3(Web3.HTTPProvider(s.mantle_rpc))
    acct = Account.from_key(s.mantle_private_key)
    tx = {
        "to": Web3.to_checksum_address(pay_to),
        "value": amount,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": int(s.x402_chain_id),
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    tx_hash = h.hex() if hasattr(h, "hex") else str(h)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    print(f"paid: {tx_hash}  — waiting for confirmation…")
    w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print(f"confirmed: https://sepolia.mantlescan.xyz/tx/{tx_hash}")

    # 3. retry with proof of payment
    payment = {"x402Version": 1, "scheme": "mnt-native", "network": req["network"],
               "payload": {"txHash": tx_hash, "from": acct.address}}
    status, data = _get(url, {"X-PAYMENT": _b64(payment)})
    print(f"\nretry with X-PAYMENT → HTTP {status}")
    print(json.dumps(data, indent=2)[:1400])
    if status == 200:
        print("\n✅ paid x402 round-trip OK — native-MNT settlement on Mantle works end to end.")


if __name__ == "__main__":
    main()
