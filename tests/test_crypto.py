"""Tests for the Fernet-based clipboard encryption helpers."""

from __future__ import annotations

import pytest

from clipsync import crypto


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


def test_decrypt_wrong_passphrase_returns_none() -> None:
    token = crypto.encrypt(b"secret", "right")
    assert crypto.decrypt(token, "wrong") is None


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


def test_is_encrypted_detects_v0_and_v1() -> None:
    v1 = crypto.encrypt(b"x", "pw")
    v0 = crypto._ENC_MAGIC_V0 + b"legacy-token-bytes"
    assert crypto.is_encrypted(v1)
    assert crypto.is_encrypted(v0)
    assert not crypto.is_encrypted(b"plain text")
    assert not crypto.is_encrypted(b"")


def test_each_encrypt_uses_random_salt() -> None:
    """Two encrypt() calls with the same input must produce different ciphertext."""
    a = crypto.encrypt(b"same", "pw")
    b = crypto.encrypt(b"same", "pw")
    assert a != b
    # Both must still decrypt to the same plaintext.
    assert crypto.decrypt(a, "pw") == b"same"
    assert crypto.decrypt(b, "pw") == b"same"


def test_v0_legacy_payload_decrypts() -> None:
    """v0 used a hardcoded salt; current code must still read those payloads."""
    # We can't call crypto.encrypt() to make a v0 (it always emits v1), so
    # construct one by hand: magic + Fernet token derived with the legacy salt.
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=crypto._LEGACY_SALT, iterations=120_000)
    key = base64.urlsafe_b64encode(kdf.derive(b"pw"))
    token = Fernet(key).encrypt(b"legacy data")
    v0_payload = crypto._ENC_MAGIC_V0 + token

    assert crypto.is_encrypted(v0_payload)
    assert crypto.decrypt(v0_payload, "pw") == b"legacy data"
    # A v0 payload encrypted with "pw" must not decrypt with a different passphrase.
    assert crypto.decrypt(v0_payload, "other") is None


@pytest.mark.parametrize("passphrase", ["", " ", "p", "long " * 100, "ünïcödé"])
def test_roundtrip_various_passphrases(passphrase: str) -> None:
    payload = b"x"
    # An empty passphrase is the "no encryption" path elsewhere; encrypt()
    # still has to work if asked directly.
    token = crypto.encrypt(payload, passphrase)
    assert crypto.decrypt(token, passphrase) == payload
