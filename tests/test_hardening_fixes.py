from __future__ import annotations

from pathlib import Path

import pytest

from clipsync import config
from clipsync.clipboard import ClipboardSync
from clipsync.crypto import decrypt, is_encrypted
from clipsync.file_transfer import FileTransfer


class _Settings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def _sync(tmp_path: Path, passphrase: str = "") -> ClipboardSync:
    monkey_settings = config.Settings(path=tmp_path / "settings.json")
    folder = tmp_path / "sync"
    monkey_settings.set("sync_folder", str(folder))
    monkey_settings.set("encryption_passphrase", passphrase)
    return ClipboardSync(monkey_settings)


def test_inbound_clipboard_write_failure_is_retryable(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    sync.clipboard_file.parent.mkdir(parents=True, exist_ok=True)
    sync.clipboard_file.write_text("remote value", encoding="utf-8")
    calls = []

    def write(value: str) -> bool:
        calls.append(value)
        return len(calls) > 1

    monkeypatch.setattr(sync, "_write_clipboard", write)
    sync._on_text_file_changed()
    assert sync._last_synced is None
    sync._on_text_file_changed()
    assert sync._last_synced == "remote value"
    assert calls == ["remote value", "remote value"]


def test_passphrase_change_reencrypts_existing_clipboard(tmp_path):
    sync = _sync(tmp_path, "old")
    sync._write_file("secret text")
    assert is_encrypted(sync.clipboard_file.read_bytes())

    sync._settings.set("encryption_passphrase", "new")
    sync.reconcile_encryption()
    payload = sync.clipboard_file.read_bytes()
    assert decrypt(payload, "new") == b"secret text"
    assert decrypt(payload, "old") is None


def test_clearing_passphrase_restores_plaintext_file(tmp_path):
    sync = _sync(tmp_path, "old")
    sync._write_file("secret text")
    sync._settings.set("encryption_passphrase", "")
    sync.reconcile_encryption()
    assert sync.clipboard_file.read_bytes() == b"secret text"


def test_file_transfer_names_are_unique_even_with_same_timestamp(tmp_path, monkeypatch):
    settings = _Settings(sync_folder=str(tmp_path / "sync"), encryption_passphrase="")
    transfer = FileTransfer(settings, on_received=lambda *_args: None, device_id="A" * 56)
    source = tmp_path / "same.txt"
    source.write_bytes(b"data")
    monkeypatch.setattr("clipsync.file_transfer.time.strftime", lambda *_args: "20260101_000000")

    first = transfer.send(source)
    second = transfer.send(source)
    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"data"
    assert first.parent.name.endswith("-AAAAAAA")


def test_file_transfer_rejects_oversized_files(tmp_path, monkeypatch):
    import clipsync.file_transfer as file_transfer

    settings = _Settings(sync_folder=str(tmp_path / "sync"), encryption_passphrase="")
    transfer = FileTransfer(settings, on_received=lambda *_args: None)
    source = tmp_path / "large.bin"
    source.write_bytes(b"x")
    monkeypatch.setattr(file_transfer, "MAX_FILE_SIZE", 0)
    with pytest.raises(ValueError, match="too large"):
        transfer.send(source)
