"""CredentialCodec supports master-key rotation via MultiFernet: a comma-separated
key list decrypts ciphertext sealed under any listed key and seals new ciphertext
under the newest, so the master key can be rotated without losing stored secrets."""
import pytest
from cryptography.fernet import InvalidToken

from perpsagent.adapters.store.credentials import CredentialCodec

_CREDS = {"api_key": "x", "api_secret": "y", "testnet": True}


def test_single_key_roundtrip_unchanged():
    c = CredentialCodec(CredentialCodec.generate_key())
    assert c.decrypt(c.encrypt(_CREDS)) == _CREDS


def test_rotation_decrypts_old_key_and_reseals_to_new():
    old, new = CredentialCodec.generate_key(), CredentialCodec.generate_key()
    blob = CredentialCodec(old).encrypt(_CREDS)

    rotated = CredentialCodec(f"{new},{old}")   # new primary, old still accepted
    assert rotated.decrypt(blob) == _CREDS       # old ciphertext still readable

    migrated = rotated.reseal(blob)              # re-encrypt under the new primary
    assert CredentialCodec(new).decrypt(migrated) == _CREDS


def test_new_only_codec_cannot_read_old_blob():
    old, new = CredentialCodec.generate_key(), CredentialCodec.generate_key()
    blob = CredentialCodec(old).encrypt(_CREDS)
    with pytest.raises(InvalidToken):            # proves rotation is actually required
        CredentialCodec(new).decrypt(blob)


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        CredentialCodec("   ")
