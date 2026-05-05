"""Clipboard encryption helpers.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with PBKDF2-HMAC-SHA256 key derivation.
Supports two payload versions:

  v0: hardcoded salt (legacy, kept for reading old payloads)
  v1: random 16-byte salt per payload (current)
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)

_ENC_MAGIC_V0: Final = b"CSENC\x00"
_ENC_MAGIC_V1: Final = b"CSENC\x01"
_SALT_LEN: Final = 16
_LEGACY_SALT: Final = b"clipsync-v1-salt"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=120_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt(payload: bytes, passphrase: str) -> bytes:
    """Encrypt arbitrary bytes with a random salt and return a versioned payload."""
    salt = os.urandom(_SALT_LEN)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(payload)
    return _ENC_MAGIC_V1 + salt + token


def decrypt(data: bytes, passphrase: str) -> bytes | None:
    """Decrypt a versioned payload. Returns raw bytes, or None on failure."""
    if data.startswith(_ENC_MAGIC_V1):
        header_len = len(_ENC_MAGIC_V1)
        if len(data) < header_len + _SALT_LEN + 1:
            return None
        salt = data[header_len : header_len + _SALT_LEN]
        token = data[header_len + _SALT_LEN :]
        try:
            return Fernet(_derive_key(passphrase, salt)).decrypt(token)
        except (InvalidToken, ValueError):
            return None

    if data.startswith(_ENC_MAGIC_V0):
        token = data[len(_ENC_MAGIC_V0) :]
        try:
            return Fernet(_derive_key(passphrase, _LEGACY_SALT)).decrypt(token)
        except (InvalidToken, ValueError):
            return None

    return None


def is_encrypted(data: bytes) -> bool:
    """Return True if *data* starts with a recognized encryption header."""
    return data.startswith(_ENC_MAGIC_V0) or data.startswith(_ENC_MAGIC_V1)
