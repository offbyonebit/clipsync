"""Clipboard encryption helpers.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with PBKDF2-HMAC-SHA256 key derivation.
Supports three payload versions:

  v0: hardcoded salt, 120k iterations (legacy, kept for reading old payloads)
  v1: random 16-byte salt per payload, 120k iterations (older releases)
  v2: random 16-byte salt per payload, 600k iterations (current)

v2 raises the PBKDF2 iteration count to the OWASP-recommended floor for
PBKDF2-HMAC-SHA256 (>=600,000). New writes produce v2; v0 and v1 payloads
written by older releases still decrypt transparently (re-derived with
their original iteration counts). The iteration count is fixed per
version and not stored in the payload, so a future bump adds a new magic.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)

# Version-agnostic prefix. is_encrypted() matches on this rather than on the
# known magics so that a payload written by a NEWER build is still recognized
# as ciphertext by an older one. decrypt() will return None for a version it
# does not know, and the caller must then refuse to overwrite the file rather
# than treating it as plaintext and destroying the peer's data.
_ENC_MAGIC_PREFIX: Final = b"CSENC"
_ENC_MAGIC_V0: Final = _ENC_MAGIC_PREFIX + b"\x00"
_ENC_MAGIC_V1: Final = _ENC_MAGIC_PREFIX + b"\x01"
_ENC_MAGIC_V2: Final = _ENC_MAGIC_PREFIX + b"\x02"
_SALT_LEN: Final = 16
_LEGACY_SALT: Final = b"clipsync-v1-salt"

# PBKDF2-HMAC-SHA256 iteration counts, pinned per payload version. v2 follows
# the OWASP-recommended floor (>=600,000). Both derive paths run on the
# background sync threads (~65 ms at 600k), never on the UI/main thread.
_PBKDF2_ITERATIONS_LEGACY: Final = 120_000
_PBKDF2_ITERATIONS_V2: Final = 600_000


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt(payload: bytes, passphrase: str) -> bytes:
    """Encrypt arbitrary bytes with a random salt and return a v2 payload.

    v2 derives the Fernet key with 600,000 PBKDF2-SHA256 iterations.
    """
    salt = os.urandom(_SALT_LEN)
    token = Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS_V2)).encrypt(payload)
    return _ENC_MAGIC_V2 + salt + token


def decrypt(data: bytes, passphrase: str) -> bytes | None:
    """Decrypt a versioned payload. Returns raw bytes, or None on failure.

    Handles v2 (600k iterations), v1 (120k, random salt), and v0 (120k,
    hardcoded legacy salt) so payloads written by older releases remain
    readable after upgrade.
    """
    if data.startswith(_ENC_MAGIC_V2):
        header_len = len(_ENC_MAGIC_V2)
        if len(data) < header_len + _SALT_LEN + 1:
            return None
        salt = data[header_len : header_len + _SALT_LEN]
        token = data[header_len + _SALT_LEN :]
        try:
            return Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS_V2)).decrypt(token)
        except (InvalidToken, ValueError):
            return None

    if data.startswith(_ENC_MAGIC_V1):
        header_len = len(_ENC_MAGIC_V1)
        if len(data) < header_len + _SALT_LEN + 1:
            return None
        salt = data[header_len : header_len + _SALT_LEN]
        token = data[header_len + _SALT_LEN :]
        try:
            return Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS_LEGACY)).decrypt(token)
        except (InvalidToken, ValueError):
            return None

    if data.startswith(_ENC_MAGIC_V0):
        token = data[len(_ENC_MAGIC_V0) :]
        try:
            return Fernet(_derive_key(passphrase, _LEGACY_SALT, _PBKDF2_ITERATIONS_LEGACY)).decrypt(token)
        except (InvalidToken, ValueError):
            return None

    return None


# ---------------------------------------------------------------------------
# Streaming file encryption
# ---------------------------------------------------------------------------
#
# Fernet holds the whole payload in memory, so the clipboard helpers above are
# unusable for file transfer: a multi-GB send would exhaust RAM. Files are
# therefore written as a sequence of independently-authenticated chunks.
#
# Layout:
#     CSENCF\x01            magic + version (distinct from the CSENC clipboard
#                           payloads so the two can never be confused)
#     salt                  16 random bytes, PBKDF2 as v2 (600k iterations)
#     repeated:
#         4-byte big-endian token length
#         Fernet token over (8-byte big-endian chunk index || chunk bytes)
#
# The index is inside the authenticated plaintext, so chunks cannot be
# reordered or dropped without detection. The stream ends with an
# empty-payload chunk acting as an EOF marker, so truncation is caught too:
# without it, a cut-short file would otherwise decrypt cleanly to a prefix.
_FILE_MAGIC: Final = b"CSENCF\x01"
_FILE_CHUNK_SIZE: Final = 1024 * 1024


class StreamDecryptError(Exception):
    """Raised when an encrypted file cannot be decrypted or fails integrity."""


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    """Encrypt *src* into *dst* in bounded memory."""
    salt = os.urandom(_SALT_LEN)
    fernet = Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS_V2))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(_FILE_MAGIC)
        fout.write(salt)
        index = 0
        while True:
            chunk = fin.read(_FILE_CHUNK_SIZE)
            if not chunk:
                break
            token = fernet.encrypt(index.to_bytes(8, "big") + chunk)
            fout.write(len(token).to_bytes(4, "big"))
            fout.write(token)
            index += 1
        # EOF marker: an authenticated empty chunk at the next index.
        token = fernet.encrypt(index.to_bytes(8, "big"))
        fout.write(len(token).to_bytes(4, "big"))
        fout.write(token)


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    """Decrypt *src* into *dst*. Raises StreamDecryptError on any failure.

    *dst* is removed on failure so a partial plaintext is never left behind
    for the user to mistake for a complete file.
    """
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            if fin.read(len(_FILE_MAGIC)) != _FILE_MAGIC:
                raise StreamDecryptError("not a clipsync encrypted file")
            salt = fin.read(_SALT_LEN)
            if len(salt) != _SALT_LEN:
                raise StreamDecryptError("truncated header")
            fernet = Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS_V2))
            expected = 0
            saw_eof = False
            while True:
                raw_len = fin.read(4)
                if not raw_len:
                    break
                if len(raw_len) != 4:
                    raise StreamDecryptError("truncated chunk length")
                token = fin.read(int.from_bytes(raw_len, "big"))
                try:
                    plain = fernet.decrypt(token)
                except (InvalidToken, ValueError) as exc:
                    raise StreamDecryptError("wrong passphrase or corrupt data") from exc
                if len(plain) < 8:
                    raise StreamDecryptError("corrupt chunk")
                if int.from_bytes(plain[:8], "big") != expected:
                    raise StreamDecryptError("chunks reordered or dropped")
                body = plain[8:]
                if not body:
                    saw_eof = True
                    break
                fout.write(body)
                expected += 1
            if not saw_eof:
                raise StreamDecryptError("file is truncated")
    except StreamDecryptError:
        dst.unlink(missing_ok=True)
        raise
    except OSError:
        dst.unlink(missing_ok=True)
        raise


def is_encrypted_file(path: Path) -> bool:
    """True if *path* begins with the streaming-file magic."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_FILE_MAGIC)) == _FILE_MAGIC
    except OSError:
        return False


def is_encrypted(data: bytes) -> bool:
    """Return True if *data* looks like a CSENC payload of ANY version.

    Deliberately matches the version-agnostic prefix, including versions this
    build cannot decrypt. Callers use this to decide whether a file is safe to
    overwrite, and an unknown future version is exactly the case where it is
    not.
    """
    return data.startswith(_ENC_MAGIC_PREFIX)
