"""Guard against destroying a peer's ciphertext (issue #21).

These are synchronous unit tests: no start(), no Observer, no threads. They
poke _write_file / _write_image_file / _out_tick directly.
"""

from __future__ import annotations

import pytest

from clipsync import config, crypto
from clipsync.clipboard import ClipboardSync, EncryptedPayloadError


@pytest.fixture(autouse=True)
def _isolate_history(tmp_path, monkeypatch):
    """ClipboardSync builds a ClipboardHistory bound to config.HISTORY_FILE at
    import-time module scope. Without this the tests read and rewrite the
    developer's real clipboard history."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")


def _make_sync(tmp_path, passphrase=None):
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    settings = config.Settings(path=tmp_path / "settings.json")
    settings.set("sync_folder", str(sync_folder))
    if passphrase is not None:
        settings.set("encryption_passphrase", passphrase)
    return ClipboardSync(settings)


def test_write_file_refuses_ciphertext_without_passphrase(tmp_path):
    sync = _make_sync(tmp_path)
    ciphertext = crypto.encrypt(b"peer text", "peer-secret")
    sync.clipboard_file.write_bytes(ciphertext)
    original = sync.clipboard_file.read_bytes()

    with pytest.raises(EncryptedPayloadError):
        sync._write_file("local text")

    assert sync.clipboard_file.read_bytes() == original


def test_write_image_file_refuses_ciphertext_without_passphrase(tmp_path):
    sync = _make_sync(tmp_path)
    ciphertext = crypto.encrypt(b"peer image", "peer-secret")
    sync.clipboard_image_file.write_bytes(ciphertext)
    original = sync.clipboard_image_file.read_bytes()

    with pytest.raises(EncryptedPayloadError):
        sync._write_image_file(b"local image")

    assert sync.clipboard_image_file.read_bytes() == original


def test_write_file_refuses_ciphertext_with_wrong_passphrase(tmp_path):
    sync = _make_sync(tmp_path, passphrase="local-secret")
    ciphertext = crypto.encrypt(b"peer text", "peer-secret")
    sync.clipboard_file.write_bytes(ciphertext)
    original = sync.clipboard_file.read_bytes()

    with pytest.raises(EncryptedPayloadError):
        sync._write_file("local text")

    assert sync.clipboard_file.read_bytes() == original


def test_write_file_succeeds_on_decryptable_ciphertext(tmp_path):
    passphrase = "shared-secret"
    sync = _make_sync(tmp_path, passphrase=passphrase)
    ciphertext = crypto.encrypt(b"peer text", passphrase)
    sync.clipboard_file.write_bytes(ciphertext)

    sync._write_file("local text")

    data = sync.clipboard_file.read_bytes()
    assert crypto.decrypt(data, passphrase) == b"local text"


def test_write_file_succeeds_on_plaintext_or_missing_file(tmp_path):
    sync = _make_sync(tmp_path)
    sync._write_file("first text")
    assert sync.clipboard_file.read_bytes() == b"first text"

    sync._write_file("second text")
    assert sync.clipboard_file.read_bytes() == b"second text"


def test_out_tick_restores_last_synced_on_encrypted_payload_error(tmp_path):
    sync = _make_sync(tmp_path)
    ciphertext = crypto.encrypt(b"peer text", "peer-secret")
    sync.clipboard_file.write_bytes(ciphertext)
    sync._last_synced = "prior value"
    sync._read_clipboard = lambda: "local text"
    sync._read_clipboard_image = lambda: None

    sync._out_tick()

    assert sync._last_synced == "prior value"
    assert sync.clipboard_file.read_bytes() == ciphertext


def test_write_file_refuses_future_ciphertext_version(tmp_path):
    sync = _make_sync(tmp_path)
    sync.clipboard_file.write_bytes(b"CSENC\xff")
    original = sync.clipboard_file.read_bytes()

    with pytest.raises(EncryptedPayloadError):
        sync._write_file("local text")

    assert sync.clipboard_file.read_bytes() == original
