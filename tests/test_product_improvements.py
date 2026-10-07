"""Regression coverage for privacy, history, status, and transfer lifecycle."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from clipsync import config
from clipsync.clipboard import ClipboardSync
from clipsync.file_transfer import FileTransfer, _FileReceiveHandler
from clipsync.history import ClipboardHistory


def _settings(tmp_path) -> config.Settings:
    settings = config.Settings(tmp_path / "settings.json")
    settings.set("sync_folder", str(tmp_path / "sync"))
    return settings


def test_idle_history_expiration_is_persisted(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("history_auto_clear_minutes", 1)
    history = ClipboardHistory(settings)
    monkeypatch.setattr("clipsync.history.time.time", lambda: 1_000.0)
    history.add_entry("expires", "local")

    monkeypatch.setattr("clipsync.history.time.time", lambda: 1_061.0)
    assert history.expire_now()
    assert history.get_entries() == []
    assert ClipboardHistory(settings).get_entries() == []


def test_pinned_history_still_obeys_privacy_expiration(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("history_auto_clear_minutes", 1)
    history = ClipboardHistory(settings)
    monkeypatch.setattr("clipsync.history.time.time", lambda: 1_000.0)
    history.add_entry("pinned secret")
    entry = history.get_entries()[0]
    assert history.set_pinned(entry.timestamp, True)

    monkeypatch.setattr("clipsync.history.time.time", lambda: 1_061.0)
    history.expire_now()
    assert history.get_entries() == []


def test_image_history_stores_bounded_thumbnail(tmp_path):
    settings = _settings(tmp_path)
    image = Image.new("RGB", (1200, 900), "red")
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    history = ClipboardHistory(settings)

    history.add_image(payload.getvalue(), "remote")

    entry = history.get_entries()[0]
    assert entry.kind == "image"
    assert entry.image_b64
    assert len(entry.image_b64) < 700_000


def test_manual_send_requires_explicit_send(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("manual_send", True)
    sync = ClipboardSync(settings)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "manual")
    published: list[str] = []
    monkeypatch.setattr(sync, "_write_file", published.append)

    sync._out_tick()
    assert published == []
    sync.send_current()
    assert published == ["manual"]
    assert sync.status_snapshot()["replication"] == "Waiting for Syncthing"


@pytest.mark.parametrize("secret", ["password = example-secret", "-----BEGIN OPENSSH PRIVATE KEY-----"])
def test_secret_filter_blocks_outgoing_text_and_history(tmp_path, monkeypatch, secret):
    settings = _settings(tmp_path)
    settings.set("filter_likely_secrets", True)
    sync = ClipboardSync(settings)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: secret)
    sync.send_current()

    assert not sync.clipboard_file.exists()
    assert sync._history.get_entries() == []
    assert "privacy filter" in str(sync.status_snapshot()["error"])


def test_secret_filter_blocks_incoming_and_allows_it_when_disabled(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("filter_likely_secrets", True)
    sync = ClipboardSync(settings)
    sync.clipboard_file.parent.mkdir(parents=True, exist_ok=True)
    sync.clipboard_file.write_text("api_key = example-secret", encoding="utf-8")
    written = []
    monkeypatch.setattr(sync, "_write_clipboard", lambda value: written.append(value) or True)

    sync._on_text_file_changed()
    assert written == []
    assert sync._history.get_entries() == []
    assert "filter" in str(sync.status_snapshot()["error"])

    settings.set("filter_likely_secrets", False)
    sync._on_text_file_changed()
    assert written == ["api_key = example-secret"]
    assert sync.status_snapshot()["error"] is None


def test_disabling_history_still_publishes_but_does_not_save_new_clips(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    sync = ClipboardSync(settings)
    sync._history.add_entry("existing")
    settings.set("history_enabled", False)
    sync.refresh_settings()
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "new")

    sync.send_current()

    assert sync.clipboard_file.read_text(encoding="utf-8") == "new"
    assert [entry.text for entry in sync._history.get_entries()] == ["existing"]


def test_timed_pause_reconciles_changes_when_it_expires(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("paused_until", time.time() + 60)
    sync = ClipboardSync(settings)
    reconciled: list[bool] = []
    monkeypatch.setattr(sync, "reconcile_latest", lambda: reconciled.append(True))

    assert sync._refresh_pause_state(was_paused=True)
    assert reconciled == []
    settings.set("paused_until", time.time() - 1)
    assert not sync._refresh_pause_state(was_paused=True)
    assert reconciled == [True]


def test_oversized_text_is_not_published(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.set("max_text_bytes", 1024)
    sync = ClipboardSync(settings)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "x" * 1025)
    published: list[str] = []
    monkeypatch.setattr(sync, "_write_file", published.append)

    sync._out_tick()

    assert published == []
    assert "limit" in str(sync.status_snapshot()["error"])


def test_transfer_ids_are_unique_and_ack_is_real_delivery_evidence(tmp_path):
    settings = _settings(tmp_path)
    transfer = FileTransfer(settings, on_received=lambda _path, _sender: True)
    source = tmp_path / "report.txt"
    source.write_text("hello")

    first = transfer.send(source)
    second = transfer.send(source)
    assert first != second
    transfer_id = first.name.split("_", 1)[0]
    before = next(item for item in transfer.status_snapshot() if item["id"] == transfer_id)
    assert before["status"] == "transferring"

    ack = transfer._ack_path(transfer_id)
    ack.parent.mkdir(parents=True)
    ack.write_text("received")
    after = next(item for item in transfer.status_snapshot() if item["id"] == transfer_id)
    assert after["status"] == "delivered"
    assert after["acknowledgements"] == 1


def test_transfer_waits_for_every_expected_receiver(tmp_path):
    settings = _settings(tmp_path)
    transfer = FileTransfer(
        settings,
        on_received=lambda _path, _sender: True,
        expected_receivers=lambda: 2,
    )
    source = tmp_path / "report.txt"
    source.write_text("hello")

    published = transfer.send(source)
    transfer_id = published.name.split("_", 1)[0]
    ack_dir = transfer._ack_path(transfer_id).parent
    ack_dir.mkdir(parents=True)
    (ack_dir / "laptop.ack").write_text("received")
    pending = next(item for item in transfer.status_snapshot() if item["id"] == transfer_id)
    assert pending["status"] == "transferring"
    assert pending["acknowledgements"] == 1

    (ack_dir / "desktop.ack").write_text("received")
    delivered = next(item for item in transfer.status_snapshot() if item["id"] == transfer_id)
    assert delivered["status"] == "delivered"
    assert delivered["acknowledgements"] == 2


def test_restart_recovers_unacknowledged_incoming_file(tmp_path):
    settings = _settings(tmp_path)
    transfer = FileTransfer(settings, on_received=lambda _path, _sender: True)
    incoming = transfer.files_dir / "peer-host" / "report.txt"
    incoming.parent.mkdir(parents=True)
    incoming.write_text("hello")
    handler = _FileReceiveHandler(
        on_received=transfer._enqueue_receive,
        files_dir=transfer.files_dir,
    )

    transfer._recover_pending(handler)

    path, sender, attempt = transfer._receive_queue.get_nowait()
    assert (path, sender, attempt) == (incoming, "peer-host", 0)


def test_receive_ack_waits_for_success_and_retries_without_duplicate_save(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    incoming = tmp_path / "sync" / "files" / "peer-host" / "report.txt"
    incoming.parent.mkdir(parents=True)
    incoming.write_text("hello")
    save_results = iter((False, True))
    saves: list[Path] = []

    def save(path, _sender):
        saves.append(path)
        return next(save_results)

    scheduled: list[tuple[object, tuple[object, ...]]] = []

    class ImmediateTimer:
        daemon = False

        def __init__(self, _delay, callback, args=()):
            self.callback = callback
            self.args = args

        def start(self):
            scheduled.append((self.callback, self.args))

    monkeypatch.setattr("clipsync.file_transfer.threading.Timer", ImmediateTimer)
    transfer = FileTransfer(settings, on_received=save)
    transfer._receive_once(incoming, "peer-host", 0)
    assert not transfer._ack_path(transfer._transfer_id(incoming)).exists()

    callback, args = scheduled.pop(0)
    callback(*args)
    path, sender, attempt = transfer._receive_queue.get_nowait()
    transfer._receive_once(path, sender, attempt)
    assert transfer._ack_path(transfer._transfer_id(incoming)).exists()
    assert saves == [incoming, incoming]

    real_permissions = config.set_file_permissions
    permission_attempts = 0

    def flaky_permissions(path):
        nonlocal permission_attempts
        permission_attempts += 1
        if permission_attempts == 1:
            raise OSError("temporarily read-only")
        real_permissions(path)

    transfer._ack_path(transfer._transfer_id(incoming)).unlink()
    monkeypatch.setattr(config, "set_file_permissions", flaky_permissions)
    transfer._write_ack(incoming)
    callback, args = scheduled.pop(0)
    callback(*args)
    assert transfer._ack_path(transfer._transfer_id(incoming)).exists()
    assert saves == [incoming, incoming]
