"""Tests for clipboard encryption helpers (crypto.py)."""

from __future__ import annotations

import pytest

from clipsync import crypto
from clipsync.crypto import _ENC_MAGIC_V0, _ENC_MAGIC_V1, _LEGACY_SALT, _derive_key


# ---------------------------------------------------------------------------
# Encrypt / decrypt round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_text() -> None:
    plaintext = b"hello world"
    assert crypto.decrypt(crypto.encrypt(plaintext, "secret"), "secret") == plaintext


def test_roundtrip_binary() -> None:
    data = bytes(range(256))
    assert crypto.decrypt(crypto.encrypt(data, "pass"), "pass") == data


def test_roundtrip_empty_bytes() -> None:
    assert crypto.decrypt(crypto.encrypt(b"", "pw"), "pw") == b""


def test_roundtrip_unicode_passphrase() -> None:
    plaintext = b"data"
    passphrase = "pässwörd\U0001f511"
    assert crypto.decrypt(crypto.encrypt(plaintext, passphrase), passphrase) == plaintext


# ---------------------------------------------------------------------------
# Wrong passphrase
# ---------------------------------------------------------------------------


def test_wrong_passphrase_returns_none() -> None:
    ciphertext = crypto.encrypt(b"secret data", "correct")
    assert crypto.decrypt(ciphertext, "wrong") is None


def test_empty_passphrase_wrong_returns_none() -> None:
    ciphertext = crypto.encrypt(b"data", "notempty")
    assert crypto.decrypt(ciphertext, "") is None


# ---------------------------------------------------------------------------
# V1 format properties
# ---------------------------------------------------------------------------


def test_encrypt_produces_v1_magic() -> None:
    ct = crypto.encrypt(b"x", "pw")
    assert ct.startswith(_ENC_MAGIC_V1)


def test_v1_uses_random_salt_per_call() -> None:
    ct1 = crypto.encrypt(b"same", "pw")
    ct2 = crypto.encrypt(b"same", "pw")
    assert ct1 != ct2


# ---------------------------------------------------------------------------
# V0 legacy format (backward compatibility)
# ---------------------------------------------------------------------------


def _make_v0_payload(plaintext: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    key = _derive_key(passphrase, _LEGACY_SALT)
    token = Fernet(key).encrypt(plaintext)
    return _ENC_MAGIC_V0 + token


def test_v0_legacy_decrypt() -> None:
    payload = _make_v0_payload(b"legacy data", "oldpass")
    assert crypto.decrypt(payload, "oldpass") == b"legacy data"


def test_v0_wrong_passphrase_returns_none() -> None:
    payload = _make_v0_payload(b"data", "correct")
    assert crypto.decrypt(payload, "wrong") is None


# ---------------------------------------------------------------------------
# Truncated / malformed input
# ---------------------------------------------------------------------------


def test_truncated_v1_header_returns_none() -> None:
    # V1 magic + salt that is too short (no token)
    short = _ENC_MAGIC_V1 + b"\x00" * 5
    assert crypto.decrypt(short, "pw") is None


def test_garbage_returns_none() -> None:
    assert crypto.decrypt(b"not encrypted at all", "pw") is None


def test_empty_bytes_returns_none() -> None:
    assert crypto.decrypt(b"", "pw") is None


def test_partial_magic_returns_none() -> None:
    assert crypto.decrypt(b"CSEN", "pw") is None


# ---------------------------------------------------------------------------
# is_encrypted
# ---------------------------------------------------------------------------


def test_is_encrypted_v1() -> None:
    assert crypto.is_encrypted(crypto.encrypt(b"x", "pw")) is True


def test_is_encrypted_v0() -> None:
    assert crypto.is_encrypted(_make_v0_payload(b"x", "pw")) is True


def test_is_encrypted_plaintext() -> None:
    assert crypto.is_encrypted(b"plain text clipboard") is False


def test_is_encrypted_empty() -> None:
    assert crypto.is_encrypted(b"") is False
