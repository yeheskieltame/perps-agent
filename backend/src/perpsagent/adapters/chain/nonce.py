"""NonceManager — serialized nonce allocation + non-blocking tx submission for one
on-chain signer.

The old path called `get_transaction_count('pending')` inside each `to_thread`
send, so two concurrent sends could grab the SAME nonce (collision under any
multi-user launch), and every send blocked up to 120s on its receipt. Here one
signer seeds its nonce ONCE and hands out strictly increasing nonces under a lock;
submission never waits for a receipt, so many txs pipeline (nonce N, N+1, …) and
confirm in parallel — throughput is bounded by block inclusion, not by serial
round-trips. One instance per wallet; a wallet pool is one NonceManager per lane
(docs.perpsagent.xyz).
"""
from __future__ import annotations

import asyncio


def _raw(signed) -> bytes:
    return getattr(signed, "raw_transaction", None) or signed.rawTransaction


class NonceManager:
    # Broadcast errors that mean the LOCAL counter is stale, not that the tx is
    # bad. Live 2026-06-11: two runner processes shared one key, each cached its
    # own _next, and the laggard's attests died with "nonce too low" (sequencer
    # err -32000). The cache can also run BEHIND ("nonce too high"/gaps) after a
    # tx is dropped. Either way: re-seed from the chain and retry once.
    _STALE_NONCE = ("nonce too low", "nonce too high", "invalid nonce",
                    "replacement transaction underpriced")

    def __init__(self, w3, acct, chain_id: int) -> None:
        self.w3 = w3
        self.acct = acct
        self.chain_id = chain_id
        self._next: int | None = None
        self._lock = asyncio.Lock()

    async def submit(self, fn, gas_price: int | None = None) -> str:
        """Build, sign, and broadcast `fn` with the next nonce. Returns the tx hash
        WITHOUT waiting for the receipt. Allocation + broadcast are serialized so
        nonces never collide; the nonce advances only on a successful broadcast.
        A stale-nonce broadcast error re-seeds from the chain and retries once —
        another process sharing this key may have advanced the chain nonce."""
        async with self._lock:
            try:
                return await asyncio.to_thread(self._submit_sync, fn, gas_price)
            except Exception as e:
                msg = str(e).lower()
                if not any(marker in msg for marker in self._STALE_NONCE):
                    raise
                self._next = None  # force a fresh pending-count seed
                return await asyncio.to_thread(self._submit_sync, fn, gas_price)

    def _submit_sync(self, fn, gas_price: int | None) -> str:
        if self._next is None:
            self._next = self.w3.eth.get_transaction_count(self.acct.address, "pending")
        nonce = self._next
        tx = fn.build_transaction({
            "from": self.acct.address,
            "nonce": nonce,
            "chainId": self.chain_id,
            "gasPrice": gas_price if gas_price is not None else self.w3.eth.gas_price,
        })
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(_raw(signed))
        self._next = nonce + 1  # consume the nonce only after a successful broadcast
        return h.hex() if hasattr(h, "hex") else str(h)

    async def confirm(self, tx_hash: str, timeout: float = 120) -> None:
        await asyncio.to_thread(self._confirm_sync, tx_hash, timeout)

    def _confirm_sync(self, tx_hash: str, timeout: float) -> None:
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        if rcpt["status"] != 1:
            raise RuntimeError(f"tx reverted: {tx_hash}")

    async def resync(self) -> None:
        """Re-seed the nonce from the chain (after a dropped tx / detected gap)."""
        async with self._lock:
            self._next = await asyncio.to_thread(
                self.w3.eth.get_transaction_count, self.acct.address, "pending"
            )
