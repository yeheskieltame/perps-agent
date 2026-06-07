"""Credential encryption codec + the sealed-creds client factory (no DB needed)."""
import pytest

from perpsagent.adapters.store.credentials import (
    CredentialCodec,
    credential_client_factory,
)


def _codec() -> CredentialCodec:
    return CredentialCodec(CredentialCodec.generate_key())


def test_encrypt_decrypt_roundtrip():
    codec = _codec()
    creds = {"api_key": "K", "api_secret": "S", "testnet": True}
    token = codec.encrypt(creds)
    assert codec.decrypt(token) == creds


def test_ciphertext_hides_secret():
    codec = _codec()
    token = codec.encrypt({"api_key": "PUBLICKEY", "api_secret": "TOPSECRET"})
    assert b"TOPSECRET" not in token and b"PUBLICKEY" not in token  # sealed, not cleartext


def test_wrong_key_cannot_decrypt():
    from cryptography.fernet import InvalidToken

    token = _codec().encrypt({"api_secret": "S"})
    with pytest.raises(InvalidToken):
        _codec().decrypt(token)  # a different master key


def test_tampered_token_rejected():
    from cryptography.fernet import InvalidToken

    codec = _codec()
    token = bytearray(codec.encrypt({"api_secret": "S"}))
    token[-1] ^= 0x01  # flip a bit
    with pytest.raises(InvalidToken):
        codec.decrypt(bytes(token))


class _FakeCredStore:
    def __init__(self):
        self._d: dict[int, bytes] = {}

    async def put_credentials(self, user_id, ciphertext):
        self._d[user_id] = ciphertext

    async def get_credentials(self, user_id):
        return self._d.get(user_id)


@pytest.mark.asyncio
async def test_credential_client_factory_builds_from_sealed_keys():
    codec = _codec()
    store = _FakeCredStore()
    await store.put_credentials(1, codec.encrypt({"api_key": "K1", "api_secret": "S1"}))

    seen: dict = {}

    def build_client(creds):
        seen.update(creds)
        return f"client-for-{creds['api_key']}"

    factory = credential_client_factory(store, codec, build_client)
    client = await factory(1)
    assert client == "client-for-K1" and seen == {"api_key": "K1", "api_secret": "S1"}

    with pytest.raises(KeyError):
        await factory(999)  # no stored credentials
