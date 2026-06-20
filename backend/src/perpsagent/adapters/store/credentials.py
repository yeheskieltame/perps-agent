"""Per-user venue credentials — encrypted at rest.

The product is non-custodial (each user supplies their own Bybit API keys), so to
recover a user's grids after a restart we must persist those keys. We NEVER store
them in cleartext: `CredentialCodec` seals them with Fernet (AES-128-CBC + HMAC)
under a master key held only in the environment (`PERPSAGENT_CRED_MASTER_KEY`,
gitignored — never logged, never committed). The ciphertext blob is what lands in
the store (Postgres `credentials.ciphertext`).

`credential_client_factory` composes a creds store + codec into the
`client_factory(user_id) -> ExchangePort` that AppService expects, so a worker can
rebuild any user's venue client from sealed keys.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable, Protocol


class CredentialCodec:
    """Seals per-user secrets with Fernet. `master_key` may be a SINGLE key or a
    comma-separated list (newest first) to enable rotation via MultiFernet: writes
    use the newest key, reads accept any listed key. Rotate by setting
    PERPSAGENT_CRED_MASTER_KEY="<new>,<old>", re-sealing stored blobs with
    `reseal()`, then dropping the old key."""

    def __init__(self, master_key: str | bytes) -> None:
        from cryptography.fernet import Fernet, MultiFernet

        if isinstance(master_key, bytes):
            raw_keys = [master_key]
        else:
            raw_keys = [k.strip() for k in master_key.split(",") if k.strip()]
        fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in raw_keys]
        if not fernets:
            raise ValueError("CredentialCodec needs at least one master key")
        # Single key → plain Fernet (unchanged behaviour); multiple → MultiFernet.
        self._fernet = fernets[0] if len(fernets) == 1 else MultiFernet(fernets)

    def encrypt(self, creds: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(creds, separators=(",", ":")).encode())

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))

    def reseal(self, token: bytes) -> bytes:
        """Re-encrypt a token under the PRIMARY (newest) key — for migrating old
        ciphertext during a key rotation. Identity when only one key is configured."""
        from cryptography.fernet import MultiFernet

        return self._fernet.rotate(token) if isinstance(self._fernet, MultiFernet) else token

    @staticmethod
    def generate_key() -> str:
        """A fresh master key (base64). Set it as PERPSAGENT_CRED_MASTER_KEY — losing
        it makes every stored credential unrecoverable."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()


class CredentialStorePort(Protocol):
    async def put_credentials(self, user_id: int, ciphertext: bytes) -> None: ...
    async def get_credentials(self, user_id: int) -> bytes | None: ...
    async def delete_credentials(self, user_id: int) -> None: ...


class CredentialAdmin:
    """Seal/unseal per-user venue keys for the worker's /v1/credentials endpoints.
    Reads return only non-secret metadata — the API never echoes a key back."""

    def __init__(self, store: CredentialStorePort, codec: CredentialCodec) -> None:
        self._store, self._codec = store, codec

    async def put(self, user_id: int, api_key: str, api_secret: str, testnet: bool) -> None:
        blob = self._codec.encrypt(
            {"api_key": api_key, "api_secret": api_secret, "testnet": testnet})
        await self._store.put_credentials(user_id, blob)

    async def info(self, user_id: int) -> dict | None:
        token = await self._store.get_credentials(user_id)
        if token is None:
            return None
        creds = self._codec.decrypt(token)
        return {"connected": True, "testnet": bool(creds.get("testnet", True)),
                "key_preview": creds.get("api_key", "")[:4] + "…"}

    async def delete(self, user_id: int) -> None:
        await self._store.delete_credentials(user_id)


def credential_client_factory(
    store: CredentialStorePort,
    codec: CredentialCodec,
    build_client: Callable[[dict], object],
) -> Callable[[int], Awaitable[object]]:
    """Compose sealed-creds storage + codec into an async client_factory. Raises
    KeyError if the user has no stored credentials."""

    async def factory(user_id: int) -> object:
        token = await store.get_credentials(user_id)
        if token is None:
            raise KeyError(f"no credentials for user {user_id}")
        return build_client(codec.decrypt(token))

    return factory
