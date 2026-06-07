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
    def __init__(self, master_key: str | bytes) -> None:
        from cryptography.fernet import Fernet

        self._fernet = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)

    def encrypt(self, creds: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(creds, separators=(",", ":")).encode())

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))

    @staticmethod
    def generate_key() -> str:
        """A fresh master key (base64). Set it as PERPSAGENT_CRED_MASTER_KEY — losing
        it makes every stored credential unrecoverable."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()


class CredentialStorePort(Protocol):
    async def put_credentials(self, user_id: int, ciphertext: bytes) -> None: ...
    async def get_credentials(self, user_id: int) -> bytes | None: ...


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
