"""x402 — pay-per-call settlement for the Perps Agent alpha API (docs/CONCEPT.md §7).

Implements the x402 flow (HTTP 402 Payment Required, scheme "exact", EVM):
1. Unpaid request → 402 with {"x402Version", "error", "accepts": [PaymentRequirements]}.
2. Client signs an EIP-3009 `TransferWithAuthorization` (EIP-712) for the asset
   (USDC) and retries with header `X-PAYMENT: base64(PaymentPayload)`.
3. We VERIFY the signature locally (eth_account EIP-712 recover) or via a
   facilitator, serve the response, then SETTLE:
     - "facilitator": POST {facilitator}/settle
     - "onchain":     broadcast transferWithAuthorization ourselves (web3)
     - "deferred":    verified now, batch-settled later (dev default)
   and attach `X-PAYMENT-RESPONSE: base64(result)`.

Defaults target Mantle (network "mantle-sepolia", chainId 5003); asset = the USDC
token address on the target chain (configure). Replay-protected by nonce.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

X402_VERSION = 1
SCHEME = "exact"

_EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

_TWA_ABI = [{
    "name": "transferWithAuthorization", "type": "function", "stateMutability": "nonpayable",
    "inputs": [
        {"type": "address"}, {"type": "address"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "uint256"}, {"type": "bytes32"}, {"type": "uint8"}, {"type": "bytes32"}, {"type": "bytes32"},
    ],
    "outputs": [],
}]


def b64encode_json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def b64decode_json(value: str) -> Any:
    return json.loads(base64.b64decode(value).decode())


def split_signature(hexsig: str) -> tuple[int, bytes, bytes]:
    """65-byte signature -> (v, r, s) for the on-chain transferWithAuthorization call."""
    h = hexsig[2:] if hexsig.startswith("0x") else hexsig
    raw = bytes.fromhex(h)
    if len(raw) != 65:
        raise ValueError("signature must be 65 bytes")
    v = raw[64]
    if v < 27:
        v += 27
    return v, raw[0:32], raw[32:64]


class X402Gateway:
    def __init__(
        self,
        pay_to: str,
        asset: str,
        network: str = "mantle-sepolia",
        chain_id: int = 5003,
        asset_name: str = "USD Coin",
        asset_version: str = "2",
        default_price: str = "10000",  # atomic units (USDC 6dp → $0.01)
        max_timeout_seconds: int = 300,
        facilitator_url: str | None = None,
        settler_rpc: str | None = None,
        settler_key: str | None = None,
    ) -> None:
        self.pay_to = pay_to
        self.asset = asset
        self.network = network
        self.chain_id = chain_id
        self.asset_name = asset_name
        self.asset_version = asset_version
        self.default_price = default_price
        self.max_timeout_seconds = max_timeout_seconds
        self.facilitator_url = facilitator_url
        self.settler_rpc = settler_rpc
        self.settler_key = settler_key
        self._used_nonces: set[str] = set()

    # ---- challenge ----

    def requirements(self, resource: str, price: str | None = None, description: str = "") -> dict:
        return {
            "scheme": SCHEME,
            "network": self.network,
            "maxAmountRequired": str(price or self.default_price),
            "resource": resource,
            "description": description or "Perps Agent verified-alpha API",
            "mimeType": "application/json",
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset": self.asset,
            "extra": {"name": self.asset_name, "version": self.asset_version},
        }

    def challenge(self, resource: str, price: str | None = None, error: str = "X-PAYMENT header is required") -> dict:
        return {"x402Version": X402_VERSION, "error": error, "accepts": [self.requirements(resource, price)]}

    # ---- verification ----

    def _typed_data(self, auth: dict) -> dict:
        return {
            "types": _EIP712_TYPES,
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": self.asset_name,
                "version": self.asset_version,
                "chainId": self.chain_id,
                "verifyingContract": self.asset,
            },
            "message": {
                "from": auth["from"],
                "to": auth["to"],
                "value": int(auth["value"]),
                "validAfter": int(auth["validAfter"]),
                "validBefore": int(auth["validBefore"]),
                "nonce": auth["nonce"],
            },
        }

    def verify_local(self, payment: dict, req: dict) -> tuple[bool, str]:
        try:
            if payment.get("x402Version") != X402_VERSION:
                return False, "unsupported x402 version"
            if payment.get("scheme") != SCHEME or payment.get("network") != self.network:
                return False, "scheme/network mismatch"
            payload = payment["payload"]
            auth = payload["authorization"]
            if str(auth["to"]).lower() != self.pay_to.lower():
                return False, "payTo mismatch"
            if int(auth["value"]) < int(req["maxAmountRequired"]):
                return False, "insufficient amount"
            now = int(time.time())
            if not (int(auth["validAfter"]) <= now <= int(auth["validBefore"])):
                return False, "authorization expired or not yet valid"
            nonce_key = f"{str(auth['from']).lower()}:{auth['nonce']}"
            if nonce_key in self._used_nonces:
                return False, "nonce already used (replay)"

            from eth_account import Account
            from eth_account.messages import encode_typed_data

            signable = encode_typed_data(full_message=self._typed_data(auth))
            sig = payload["signature"]
            sig = sig if sig.startswith("0x") else "0x" + sig
            recovered = Account.recover_message(signable, signature=sig)
            if recovered.lower() != str(auth["from"]).lower():
                return False, "invalid signature"

            self._used_nonces.add(nonce_key)
            return True, "ok"
        except Exception as e:  # malformed payload
            return False, f"malformed payment: {e}"

    async def verify(self, payment: dict, req: dict) -> tuple[bool, str]:
        if self.facilitator_url:
            return await self._facilitator("verify", payment, req)
        return self.verify_local(payment, req)

    # ---- settlement ----

    async def settle(self, payment: dict, req: dict) -> dict:
        payer = str(payment.get("payload", {}).get("authorization", {}).get("from", ""))
        if self.facilitator_url:
            ok, info = await self._facilitator("settle", payment, req)
            return {"success": ok, "network": self.network, "payer": payer, "transaction": info if ok else None,
                    "mode": "facilitator", "error": None if ok else info}
        if self.settler_rpc and self.settler_key:
            tx = await self._settle_onchain(payment)
            return {"success": True, "network": self.network, "payer": payer, "transaction": tx, "mode": "onchain"}
        # dev default: signature verified; settle in a later batch (authorization stays valid)
        return {"success": True, "network": self.network, "payer": payer, "transaction": None, "mode": "deferred"}

    async def _settle_onchain(self, payment: dict) -> str:
        import asyncio

        def _send() -> str:
            from eth_account import Account
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.settler_rpc))
            acct = Account.from_key(self.settler_key)
            token = w3.eth.contract(address=Web3.to_checksum_address(self.asset), abi=_TWA_ABI)
            auth = payment["payload"]["authorization"]
            v, r, s = split_signature(payment["payload"]["signature"])
            fn = token.functions.transferWithAuthorization(
                Web3.to_checksum_address(auth["from"]), Web3.to_checksum_address(auth["to"]),
                int(auth["value"]), int(auth["validAfter"]), int(auth["validBefore"]),
                bytes.fromhex(auth["nonce"][2:]), v, r, s,
            )
            tx = fn.build_transaction({
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "chainId": self.chain_id,
                "gasPrice": w3.eth.gas_price,
            })
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = w3.eth.send_raw_transaction(raw)
            w3.eth.wait_for_transaction_receipt(h, timeout=120)
            return h.hex()

        return await asyncio.to_thread(_send)

    async def _facilitator(self, op: str, payment: dict, req: dict) -> tuple[bool, str]:
        try:
            import aiohttp

            body = {"x402Version": X402_VERSION, "paymentPayload": payment, "paymentRequirements": req}
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{self.facilitator_url.rstrip('/')}/{op}", json=body,
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
            if op == "verify":
                return bool(data.get("isValid")), str(data.get("invalidReason") or "ok")
            return bool(data.get("success")), str(data.get("transaction") or data.get("errorReason") or "")
        except Exception as e:
            return False, f"facilitator error: {e}"
