"""Tests for the Fernet-based clipboard encryption helpers."""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from clipsync import crypto

# ---------------------------------------------------------------------------
# Encrypt / decrypt round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_v1_payload() -> None:
    payload = b"hello clipboard"
    token = crypto.encrypt(payload, "correct horse battery staple")
    assert crypto.is_encrypted(token)
    assert crypto.decrypt(token, "correct horse battery staple") == payload


def test_roundtrip_empty_payload() -> None:
    payload = b""
    token = crypto.encrypt(payload, "pw")
    assert crypto.decrypt(token, "pw") == payload


def test_roundtrip_binary_payload() -> None:
    payload = bytes(range(256))
    token = crypto.encrypt(payload, "pw")
    assert crypto.decrypt(token, "pw") == payload


@pytest.mark.parametrize("passphrase", ["", " ", "p", "long " * 100, "ünïcödé", "pässwörd\U0001f511"])
def test_roundtrip_various_passphrases(passphrase: str) -> None:
    payload = b"x"
    # An empty passphrase is the "no encryption" path elsewhere; encrypt()
    # still has to work if asked directly.
    token = crypto.encrypt(payload, passphrase)
    assert crypto.decrypt(token, passphrase) == payload


# ---------------------------------------------------------------------------
# Wrong / corrupted input
# ---------------------------------------------------------------------------


def test_decrypt_wrong_passphrase_returns_none() -> None:
    token = crypto.encrypt(b"secret", "right")
    assert crypto.decrypt(token, "wrong") is None


def test_empty_passphrase_wrong_returns_none() -> None:
    ciphertext = crypto.encrypt(b"data", "notempty")
    assert crypto.decrypt(ciphertext, "") is None


def test_decrypt_corrupted_payload_returns_none() -> None:
    token = crypto.encrypt(b"secret", "pw")
    # Flip a byte in the body.
    corrupted = token[:-1] + bytes([token[-1] ^ 0xFF])
    assert crypto.decrypt(corrupted, "pw") is None


def test_decrypt_garbage_returns_none() -> None:
    assert crypto.decrypt(b"not a csenc payload", "pw") is None


def test_decrypt_truncated_v1_payload_returns_none() -> None:
    # Header + partial salt but no body.
    truncated = crypto._ENC_MAGIC_V1 + b"\x00\x01"
    assert crypto.decrypt(truncated, "pw") is None


def test_empty_bytes_returns_none() -> None:
    assert crypto.decrypt(b"", "pw") is None


def test_partial_magic_returns_none() -> None:
    assert crypto.decrypt(b"CSEN", "pw") is None


# ---------------------------------------------------------------------------
# Format properties
# ---------------------------------------------------------------------------


def test_encrypt_produces_v2_magic() -> None:
    ct = crypto.encrypt(b"x", "pw")
    assert ct.startswith(crypto._ENC_MAGIC_V2)


def test_each_encrypt_uses_random_salt() -> None:
    """Two encrypt() calls with the same input must produce different ciphertext."""
    a = crypto.encrypt(b"same", "pw")
    b = crypto.encrypt(b"same", "pw")
    assert a != b
    # Both must still decrypt to the same plaintext.
    assert crypto.decrypt(a, "pw") == b"same"
    assert crypto.decrypt(b, "pw") == b"same"


# ---------------------------------------------------------------------------
# is_encrypted
# ---------------------------------------------------------------------------


def test_is_encrypted_detects_all_versions() -> None:
    v2 = crypto.encrypt(b"x", "pw")
    v1 = crypto._ENC_MAGIC_V1 + b"\x00" * 16 + b"token"
    v0 = crypto._ENC_MAGIC_V0 + b"legacy-token-bytes"
    assert crypto.is_encrypted(v2)
    assert crypto.is_encrypted(v1)
    assert crypto.is_encrypted(v0)
    assert not crypto.is_encrypted(b"plain text")
    assert not crypto.is_encrypted(b"")


# ---------------------------------------------------------------------------
# V0 legacy format (backward compatibility)
# ---------------------------------------------------------------------------


def _make_v0_payload(plaintext: bytes, passphrase: str) -> bytes:
    key = _derive_key(passphrase, crypto._LEGACY_SALT)
    token = Fernet(key).encrypt(plaintext)
    return crypto._ENC_MAGIC_V0 + token


def _derive_key(passphrase: str, salt: bytes, iterations: int = 120_000) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def test_v0_legacy_payload_decrypts() -> None:
    """v0 used a hardcoded salt; current code must still read those payloads."""
    v0_payload = _make_v0_payload(b"legacy data", "pw")
    assert crypto.is_encrypted(v0_payload)
    assert crypto.decrypt(v0_payload, "pw") == b"legacy data"


def test_v0_wrong_passphrase_returns_none() -> None:
    payload = _make_v0_payload(b"data", "correct")
    assert crypto.decrypt(payload, "wrong") is None


# ---------------------------------------------------------------------------
# V1 backward compatibility (older releases wrote v1 with 120k iterations)
# ---------------------------------------------------------------------------


def _make_v1_payload(plaintext: bytes, passphrase: str, salt: bytes = b"\x01" * 16) -> bytes:
    key = _derive_key(passphrase, salt)
    token = Fernet(key).encrypt(plaintext)
    return crypto._ENC_MAGIC_V1 + salt + token


def test_v1_legacy_payload_still_decrypts() -> None:
    """A payload written by an older release (v1, 120k iterations, random
    salt) must still decrypt after the v2 iteration bump."""
    v1_payload = _make_v1_payload(b"older release data", "pw")
    assert crypto.is_encrypted(v1_payload)
    assert crypto.decrypt(v1_payload, "pw") == b"older release data"


def test_v1_wrong_passphrase_returns_none() -> None:
    payload = _make_v1_payload(b"data", "correct")
    assert crypto.decrypt(payload, "wrong") is None


def test_decrypt_truncated_v2_payload_returns_none() -> None:
    truncated = crypto._ENC_MAGIC_V2 + b"\x00\x01"
    assert crypto.decrypt(truncated, "pw") is None


# ---------------------------------------------------------------------------
# V2 iteration count (the whole point of the bump)
# ---------------------------------------------------------------------------


def test_v2_actually_uses_600k_iterations() -> None:
    """Prove v2 derives the Fernet key with 600k iterations, not 120k.

    Re-derives the key at 600k (should decrypt) and at 120k (should NOT),
    pinning the iteration count rather than just asserting round-trip.
    """
    from cryptography.fernet import InvalidToken

    payload = b"secret"
    ct = crypto.encrypt(payload, "pw")
    salt = ct[len(crypto._ENC_MAGIC_V2) : len(crypto._ENC_MAGIC_V2) + crypto._SALT_LEN]
    token = ct[len(crypto._ENC_MAGIC_V2) + crypto._SALT_LEN :]

    key_600k = _derive_key("pw", salt, iterations=600_000)
    assert Fernet(key_600k).decrypt(token) == payload

    key_120k = _derive_key("pw", salt, iterations=120_000)
    with pytest.raises(InvalidToken):
        Fernet(key_120k).decrypt(token)
