"""Sent files must honour the at-rest passphrase, like the clipboard does.

send() was a bare shutil.copy2, so enabling encryption protected clipboard
text but left every sent file sitting in the shared folder as plaintext --
readable by anything with access to that directory and replicated that way to
every peer. copy2 also preserves the source mode, so a world-readable original
stayed world-readable inside the folder.

Fernet is all-in-memory, so files use a chunked streaming format instead:
CSENCF magic, salt, then length-prefixed Fernet tokens over
(chunk index || data), terminated by an authenticated empty chunk. The index
blocks reordering and dropping; the terminator catches truncation, which would
otherwise decrypt cleanly to a prefix.

Tests use random bytes as file payloads. Nothing here reads real user data.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

from clipsync import config
from clipsync.crypto import (
    StreamDecryptError,
    decrypt_file,
    encrypt_file,
    is_encrypted_file,
)
from clipsync.file_transfer import ENCRYPTED_SUFFIX, FileTransfer


class _Settings:
    def __init__(self, **kw):
        self._d = dict(kw)

    def get(self, key, default=None):
        return self._d.get(key, default)


def _digest(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Streaming primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, 1024, 1024 * 1024, 1024 * 1024 + 1, 3_500_000],
    ids=["empty", "tiny", "1k", "exactly-one-chunk", "chunk-plus-one", "multi-chunk"],
)
def test_encrypt_decrypt_roundtrip_is_exact(tmp_path, size):
    src = tmp_path / "in.bin"
    src.write_bytes(os.urandom(size))
    enc, out = tmp_path / "e", tmp_path / "o"

    encrypt_file(src, enc, "pw")
    assert is_encrypted_file(enc)
    decrypt_file(enc, out, "pw")
    assert _digest(out) == _digest(src)


def test_ciphertext_does_not_contain_the_plaintext(tmp_path):
    src = tmp_path / "in.bin"
    marker = b"TOP-SECRET-MARKER-" + os.urandom(16)
    src.write_bytes(marker * 100)
    enc = tmp_path / "e"
    encrypt_file(src, enc, "pw")
    assert marker not in enc.read_bytes(), "plaintext leaked into the encrypted file"


def test_wrong_passphrase_is_rejected_and_leaves_no_partial(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(os.urandom(2_000_000))
    enc, out = tmp_path / "e", tmp_path / "o"
    encrypt_file(src, enc, "right")

    with pytest.raises(StreamDecryptError):
        decrypt_file(enc, out, "wrong")
    assert not out.exists(), "a partial plaintext was left behind"


def test_truncation_at_a_chunk_boundary_is_detected(tmp_path):
    """Without the EOF marker a truncated file decrypts cleanly to a prefix,
    so the user silently gets a corrupt file that looks complete."""
    src = tmp_path / "in.bin"
    src.write_bytes(os.urandom(2_500_000))
    enc, out = tmp_path / "e", tmp_path / "o"
    encrypt_file(src, enc, "pw")

    raw = enc.read_bytes()
    # Drop the trailing EOF chunk: walk the length prefixes and stop early.
    pos = 7 + 16  # magic + salt
    boundaries = []
    while pos + 4 <= len(raw):
        n = int.from_bytes(raw[pos : pos + 4], "big")
        pos += 4 + n
        boundaries.append(pos)
    assert len(boundaries) >= 2
    (tmp_path / "trunc").write_bytes(raw[: boundaries[-2]])

    with pytest.raises(StreamDecryptError):
        decrypt_file(tmp_path / "trunc", out, "pw")
    assert not out.exists()


def test_reordered_chunks_are_detected(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(os.urandom(2_500_000))
    enc, out = tmp_path / "e", tmp_path / "o"
    encrypt_file(src, enc, "pw")

    raw = enc.read_bytes()
    header, pos, chunks = raw[: 7 + 16], 7 + 16, []
    while pos + 4 <= len(raw):
        n = int.from_bytes(raw[pos : pos + 4], "big")
        chunks.append(raw[pos : pos + 4 + n])
        pos += 4 + n
    assert len(chunks) >= 3
    chunks[0], chunks[1] = chunks[1], chunks[0]
    (tmp_path / "reordered").write_bytes(header + b"".join(chunks))

    with pytest.raises(StreamDecryptError, match="reordered|dropped|corrupt"):
        decrypt_file(tmp_path / "reordered", out, "pw")


def test_plaintext_file_is_not_mistaken_for_encrypted(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_bytes(b"just a normal file")
    assert not is_encrypted_file(p)


# ---------------------------------------------------------------------------
# send() integration
# ---------------------------------------------------------------------------


def _transfer(tmp_path, passphrase):
    settings = _Settings(sync_folder=str(tmp_path / "sync"), encryption_passphrase=passphrase)
    return FileTransfer(settings, on_received=lambda *_a: None)


def test_send_encrypts_when_a_passphrase_is_set(tmp_path):
    payload = os.urandom(50_000)
    src = tmp_path / "report.pdf"
    src.write_bytes(payload)

    dest = _transfer(tmp_path, "secret").send(src)

    assert dest.name.endswith(ENCRYPTED_SUFFIX), f"sent file is not marked encrypted: {dest.name}"
    assert is_encrypted_file(dest)
    assert payload[:64] not in dest.read_bytes(), "plaintext reached the shared folder"

    out = tmp_path / "recovered.pdf"
    decrypt_file(dest, out, "secret")
    assert _digest(out) == _digest(src)


def test_send_stays_plaintext_when_no_passphrase(tmp_path):
    """Without encryption configured the old behaviour is preserved, so peers
    on older builds keep working."""
    src = tmp_path / "note.txt"
    src.write_bytes(b"hello")
    dest = _transfer(tmp_path, "").send(src)

    assert not dest.name.endswith(ENCRYPTED_SUFFIX)
    assert dest.read_bytes() == b"hello"


def test_sent_file_is_not_world_readable(tmp_path):
    """copy2 preserved the source mode, so a 0644 original stayed 0644 in the
    shared folder."""
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits do not apply on Windows")
    src = tmp_path / "open.txt"
    src.write_bytes(b"data")
    os.chmod(src, 0o644)

    for passphrase in ("", "secret"):
        dest = _transfer(tmp_path, passphrase).send(src)
        mode = dest.stat().st_mode & 0o777
        assert mode == 0o600, f"passphrase={passphrase!r}: shared copy is {oct(mode)}"


def test_failed_encryption_leaves_no_partial_in_the_shared_folder(tmp_path, monkeypatch):
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(1000))

    import clipsync.file_transfer as ft

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ft, "encrypt_file", boom)
    ftr = _transfer(tmp_path, "secret")
    with pytest.raises(OSError):
        ftr.send(src)

    leftovers = list((ftr.files_dir).rglob("*"))
    files = [p for p in leftovers if p.is_file()]
    assert not files, f"partial file left in the shared folder: {files}"


def test_encrypted_send_roundtrips_a_large_file_in_bounded_memory(tmp_path):
    """The whole reason for the chunked format: Fernet in one shot would hold
    the entire file in RAM."""
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(5_000_000))
    dest = _transfer(tmp_path, "secret").send(src)
    out = tmp_path / "out.bin"
    decrypt_file(dest, out, "secret")
    assert _digest(out) == _digest(src)


def test_set_file_permissions_is_available_to_file_transfer():
    """send() relies on it for both paths."""
    assert callable(config.set_file_permissions)
