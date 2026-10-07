"""Ordering and retry regressions for the shared native clipboard."""

from __future__ import annotations

import os
import threading
import time

import pytest

from clipsync import config
from clipsync.clipboard import ClipboardSync, _ClipboardFileHandler

PNG_A = b"\x89PNG\r\n\x1a\n" + b"a"
PNG_B = b"\x89PNG\r\n\x1a\n" + b"b"


def _sync(tmp_path) -> ClipboardSync:
    folder = tmp_path / "sync"
    folder.mkdir()
    settings = config.Settings(tmp_path / "settings.json")
    settings.set("sync_folder", str(folder))
    return ClipboardSync(settings)


def test_failed_incoming_write_retries_and_commits_only_on_success(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    outcomes = iter((False, True))
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: next(outcomes))

    sync._stage_incoming("remote", "text")
    assert sync._last_synced is None
    assert sync._pending_incoming is not None

    sync._pending_incoming.next_attempt = 0
    sync._attempt_pending_incoming()
    assert sync._last_synced == "remote"
    assert sync._pending_incoming is None


def test_incoming_survives_prolonged_failure_and_eventually_applies(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: False)
    monkeypatch.setattr("clipsync.clipboard.time.monotonic", lambda: 100.0)
    sync._stage_incoming("remote", "text")

    for _ in range(20):
        assert sync._pending_incoming is not None
        sync._pending_incoming.next_attempt = 0
        sync._attempt_pending_incoming()
        assert sync._pending_incoming is not None
        assert 100.0 < sync._pending_incoming.next_attempt <= 130.0
    assert sync._last_synced is None
    assert sync._history.get_entries() == []

    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: True)
    assert sync.retry_incoming_now()
    assert sync._pending_incoming is None
    assert sync._last_synced == "remote"
    assert sync._history.get_entries()[0].text == "remote"
    assert sync.status_snapshot()["error"] is None


def test_manual_incoming_retry_obeys_pause(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: False)
    sync._stage_incoming("remote", "text")
    sync._settings.set("sync_paused", True)
    written = []
    monkeypatch.setattr(sync, "_write_clipboard", lambda value: written.append(value) or True)

    assert not sync.retry_incoming_now()
    assert written == []
    assert sync._pending_incoming is not None


def test_new_local_copy_invalidates_old_remote_retry(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    sync._last_observed_clipboard = "local-old"
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: False)
    sync._stage_incoming("remote-old", "text")
    assert sync._pending_incoming is not None

    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "local-new")
    published: list[str] = []
    monkeypatch.setattr(sync, "_write_file", published.append)
    sync._out_tick()

    assert published == ["local-new"]
    assert sync._last_synced == "local-new"
    assert sync._pending_incoming is None


@pytest.mark.parametrize(
    ("old_value", "old_kind", "new_value", "new_kind"),
    [("old text", "text", PNG_B, "image"), (PNG_A, "image", "new text", "text")],
)
def test_new_remote_type_invalidates_retry_of_other_type(
    tmp_path, monkeypatch, old_value, old_kind, new_value, new_kind
):
    sync = _sync(tmp_path)
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: False)
    monkeypatch.setattr(sync, "_write_clipboard_image", lambda _value: False)
    sync._stage_incoming(old_value, old_kind)

    written: list[object] = []
    if new_kind == "text":
        monkeypatch.setattr(sync, "_write_clipboard", lambda value: written.append(value) or True)
    else:
        monkeypatch.setattr(sync, "_write_clipboard_image", lambda value: written.append(value) or True)
    sync._stage_incoming(new_value, new_kind)

    assert written == [new_value]
    assert sync._last_synced == new_value
    assert sync._pending_incoming is None


def test_debounce_guarantees_trailing_latest_read(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    handler = _ClipboardFileHandler(sync)
    path = str(sync.clipboard_file)
    monkeypatch.setattr(handler, "_matches", lambda _path: True)

    handler._dispatch(path)
    handler._dispatch(path)
    assert sync._in_queue.get_nowait() == path
    time.sleep(0.15)
    assert sync._in_queue.get_nowait() == path


def test_resume_chooses_newer_format_only(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    sync.clipboard_file.write_text("older text", encoding="utf-8")
    sync.clipboard_image_file.write_bytes(PNG_A)
    now = time.time_ns()
    os.utime(sync.clipboard_file, ns=(now - 2_000_000, now - 2_000_000))
    os.utime(sync.clipboard_image_file, ns=(now, now))
    text_writes: list[str] = []
    image_writes: list[bytes] = []
    monkeypatch.setattr(sync, "_write_clipboard", lambda value: text_writes.append(value) or True)
    monkeypatch.setattr(sync, "_write_clipboard_image", lambda value: image_writes.append(value) or True)

    sync.reconcile_latest()

    assert image_writes == [PNG_A]
    assert text_writes == []


def test_overlapping_outbound_read_cannot_echo_incoming_write(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    sync._last_synced = "local-old"
    sync._last_observed_clipboard = "local-old"
    read_started = threading.Event()
    release_read = threading.Event()

    def delayed_read() -> str:
        read_started.set()
        assert release_read.wait(2)
        return "local-old"

    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", delayed_read)
    published: list[str] = []
    monkeypatch.setattr(sync, "_write_file", published.append)
    monkeypatch.setattr(sync, "_write_clipboard", lambda _value: True)

    thread = threading.Thread(target=sync._out_tick)
    thread.start()
    assert read_started.wait(2)
    sync._stage_incoming("remote-new", "text")
    release_read.set()
    thread.join(2)

    assert not thread.is_alive()
    assert sync._last_synced == "remote-new"
    assert published == []
