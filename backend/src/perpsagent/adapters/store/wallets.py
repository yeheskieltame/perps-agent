"""Per-user managed MNT wallet — the user's on-chain identity for paying fees.

Telegram users authenticate by connecting Bybit keys (no wallet), but the builder
fee settles in native MNT on Mantle. So the bot mints each user a managed wallet and
seals its private key with the SAME Fernet codec as the venue credentials
(`PERPSAGENT_CRED_MASTER_KEY`) — custodial-lite, identical at-rest model. The user
tops it up from the faucet; fees are debited from it on-chain. Migrating to a
non-custodial embedded/MPC wallet later changes only this class.
"""
from __future__ import annotations

import asyncio
from typing import Protocol


class WalletStorePort(Protocol):
    async def put_wallet(self, user_id: int, ciphertext: bytes) -> None: ...
    async def get_wallet(self, user_id: int) -> bytes | None: ...


class WalletAdmin:
    def __init__(self, store: WalletStorePort, codec, rpc_url: str | None = None,
                 chain_id: int | None = None) -> None:
        self._store, self._codec = store, codec
        self._rpc_url = rpc_url
        self._chain_id = chain_id

    async def get_or_create(self, user_id: int) -> str:
        """The user's wallet address, minting + sealing one on first use (idempotent)."""
        token = await self._store.get_wallet(user_id)
        if token is not None:
            return self._codec.decrypt(token)["address"]
        from eth_account import Account

        acct = Account.create()
        blob = self._codec.encrypt({"private_key": acct.key.hex(), "address": acct.address})
        await self._store.put_wallet(user_id, blob)
        return acct.address

    async def address(self, user_id: int) -> str | None:
        token = await self._store.get_wallet(user_id)
        return self._codec.decrypt(token)["address"] if token else None

    async def _private_key(self, user_id: int) -> str | None:
        token = await self._store.get_wallet(user_id)
        return self._codec.decrypt(token)["private_key"] if token else None

    async def balance(self, user_id: int) -> int:
        """On-chain MNT balance (wei); 0 when no wallet or no RPC configured."""
        addr = await self.address(user_id)
        if not addr or not self._rpc_url:
            return 0
        return await asyncio.to_thread(self._balance_sync, addr)

    async def pay(self, user_id: int, to: str, amount: int) -> str:
        """Send `amount` wei of MNT from the user's wallet to `to` (the builder fee).
        Raises KeyError if the user has no wallet; the on-chain call raises on
        insufficient funds — the caller treats that as a skipped, non-fatal fee."""
        key = await self._private_key(user_id)
        if key is None:
            raise KeyError(f"no wallet for user {user_id}")
        return await asyncio.to_thread(self._pay_sync, key, to, int(amount))

    # ---- on-chain (split out so tests can stub without a node) ----

    def _balance_sync(self, addr: str) -> int:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        return int(w3.eth.get_balance(Web3.to_checksum_address(addr)))

    def _pay_sync(self, key: str, to: str, amount: int) -> str:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        acct = Account.from_key(key)
        tx = {
            "to": Web3.to_checksum_address(to), "value": amount,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": int(self._chain_id or w3.eth.chain_id),
            "gas": 21000, "gasPrice": w3.eth.gas_price,
        }
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        w3.eth.wait_for_transaction_receipt(h, timeout=120)
        return h.hex() if hasattr(h, "hex") else str(h)
