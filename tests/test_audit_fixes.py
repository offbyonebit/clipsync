"""Regression tests for the bugs found in the July 2026 audit pass.

Each test fails against the pre-fix code. Grouped by the defect they pin
down rather than by module, so a future reader can see what behaviour is
being protected and why it mattered.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from clipsync import clipboard as clipboard_mod
from clipsync import config, syncthing
from clipsync.clipboard import ClipboardSync, _ClipboardFileHandler
from clipsync.syncthing import SyncthingError


@pytest.fixture(autouse=True)
def _isolate_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")


def _make_sync(tmp_path):
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    settings = config.Settings(path=tmp_path / "settings.json")
    settings.set("sync_folder", str(sync_folder))
    return ClipboardSync(settings)


# ---------------------------------------------------------------------------
# A transient write failure must not strand a clipboard value forever.
# ---------------------------------------------------------------------------


def test_out_tick_retries_text_after_write_oserror(tmp_path, monkeypatch):
    """_last_synced is the "already sent" guard. Leaving it set after a failed
    write meant every later tick treated the value as synced, so a single
    transient OSError silently dropped that clipboard entry for good."""
    sync = _make_sync(tmp_path)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "important text")

    calls: list[str] = []

    def failing_write(text: str) -> None:
        calls.append(text)
        raise OSError("disk busy")

    monkeypatch.setattr(sync, "_write_file", failing_write)

    sync._out_tick()
    assert calls == ["important text"]
    # The retry is the whole point: without the rollback this second tick
    # returns early because _last_synced still holds the failed value.
    sync._out_tick()
    assert calls == ["important text", "important text"], "value was never retried after OSError"


def test_out_tick_retries_image_after_write_oserror(tmp_path, monkeypatch):
    sync = _make_sync(tmp_path)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: b"\x89PNGfake")

    calls: list[bytes] = []

    def failing_write(png: bytes) -> None:
        calls.append(png)
        raise OSError("disk busy")

    monkeypatch.setattr(sync, "_write_image_file", failing_write)

    sync._out_tick()
    sync._out_tick()
    assert len(calls) == 2, "image was never retried after OSError"


def test_out_tick_still_keeps_guard_when_write_succeeds(tmp_path, monkeypatch):
    """The rollback must not defeat the ping-pong guard on the happy path."""
    sync = _make_sync(tmp_path)
    monkeypatch.setattr(sync, "_read_clipboard_image", lambda: None)
    monkeypatch.setattr(sync, "_read_clipboard", lambda: "stable text")

    calls: list[str] = []
    monkeypatch.setattr(sync, "_write_file", lambda t: calls.append(t))

    sync._out_tick()
    sync._out_tick()
    assert calls == ["stable text"], "unchanged clipboard was re-sent"


# ---------------------------------------------------------------------------
# Text and image updates must not debounce each other.
# ---------------------------------------------------------------------------


def test_debounce_is_per_path(tmp_path, monkeypatch):
    """One shared deadline meant a clipboard.txt and clipboard.png update
    landing within 100ms suppressed each other, so only one was applied."""
    sync = _make_sync(tmp_path)
    handler = _ClipboardFileHandler(sync)

    text_path = str(sync.clipboard_file)
    image_path = str(sync.clipboard_image_file)
    monkeypatch.setattr(handler, "_matches", lambda _p: True)

    handler._dispatch(text_path)
    handler._dispatch(image_path)

    queued = []
    while not sync._in_queue.empty():
        queued.append(sync._in_queue.get_nowait())

    assert text_path in queued, "text update was dropped"
    assert image_path in queued, "image update was suppressed by the text debounce"


def test_debounce_still_suppresses_repeats_of_same_path(tmp_path, monkeypatch):
    """Per-path deadlines must still collapse Syncthing's event storms."""
    sync = _make_sync(tmp_path)
    handler = _ClipboardFileHandler(sync)
    text_path = str(sync.clipboard_file)
    monkeypatch.setattr(handler, "_matches", lambda _p: True)

    for _ in range(5):
        handler._dispatch(text_path)

    queued = []
    while not sync._in_queue.empty():
        queued.append(sync._in_queue.get_nowait())
    assert queued == [text_path], "debounce no longer collapses repeat events"


def test_debounce_expires(tmp_path, monkeypatch):
    sync = _make_sync(tmp_path)
    handler = _ClipboardFileHandler(sync)
    text_path = str(sync.clipboard_file)
    monkeypatch.setattr(handler, "_matches", lambda _p: True)

    handler._dispatch(text_path)
    # Reach back past the window rather than sleeping through it.
    handler._debounce_until = {k: v - 1.0 for k, v in handler._debounce_until.items()}
    handler._dispatch(text_path)

    queued = []
    while not sync._in_queue.empty():
        queued.append(sync._in_queue.get_nowait())
    assert len(queued) == 2, "debounce never expires"


# ---------------------------------------------------------------------------
# The heartbeat must not dump a whole image into the log.
# ---------------------------------------------------------------------------


def test_heartbeat_truncates_image_bytes():
    """The truncation guard tested isinstance(last, str), so a multi-megabyte
    PNG was repr()'d in full into the log on every heartbeat."""
    big_png = b"\x89PNG" + b"A" * 500_000
    rendered = clipboard_mod._truncate_for_log(big_png)
    assert len(rendered) < 200, f"image bytes rendered to {len(rendered)} chars"
    assert rendered.endswith("...")


def test_truncate_for_log_still_truncates_str():
    rendered = clipboard_mod._truncate_for_log("x" * 5000)
    assert len(rendered) < 200
    assert rendered.endswith("...")


def test_truncate_for_log_leaves_short_values_intact():
    assert clipboard_mod._truncate_for_log("hi") == "'hi'"
    assert clipboard_mod._truncate_for_log(None) == "None"
    assert clipboard_mod._truncate_for_log(b"hi") == "b'hi'"


# ---------------------------------------------------------------------------
# Supply chain: never run a binary we could not verify.
# ---------------------------------------------------------------------------


def test_ensure_binary_rejects_binary_that_fails_digest(tmp_path, monkeypatch):
    """A replaced binary can print whatever --version string we want to see,
    so the version check alone is not evidence of anything."""
    bin_dir = tmp_path / "syncthing"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "syncthing"
    binary.write_bytes(b"tampered binary")

    monkeypatch.setattr(config, "SYNCTHING_BIN_DIR", bin_dir)
    monkeypatch.setattr(config, "syncthing_binary_path", lambda: binary)
    monkeypatch.setattr(syncthing, "_binary_version", lambda _b: "v2.0.16")
    # Digest file records a different binary.
    (bin_dir / "syncthing.sha256").write_text("v2.0.16 " + "0" * 64 + "\n", encoding="utf-8")

    redownloaded = {"hit": False}

    def fake_download(_url):
        redownloaded["hit"] = True
        raise syncthing.URLError("offline")

    monkeypatch.setattr(syncthing, "_download", fake_download)

    with pytest.raises(SyncthingError):
        syncthing.ensure_binary("v2.0.16")
    assert redownloaded["hit"], "tampered binary was trusted instead of re-downloaded"


def test_ensure_binary_accepts_binary_matching_recorded_digest(tmp_path, monkeypatch):
    """The happy path must stay offline-safe: a good binary means no network."""
    bin_dir = tmp_path / "syncthing"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "syncthing"
    binary.write_bytes(b"the real binary")

    monkeypatch.setattr(config, "SYNCTHING_BIN_DIR", bin_dir)
    monkeypatch.setattr(config, "syncthing_binary_path", lambda: binary)
    monkeypatch.setattr(syncthing, "_binary_version", lambda _b: "v2.0.16")
    digest = syncthing._file_sha256(binary)
    (bin_dir / "syncthing.sha256").write_text(f"v2.0.16 {digest}\n", encoding="utf-8")

    def explode(_url):
        raise AssertionError("network touched despite a verified binary on disk")

    monkeypatch.setattr(syncthing, "_download", explode)

    assert syncthing.ensure_binary("v2.0.16") == binary


def test_ensure_binary_redownloads_when_digest_missing(tmp_path, monkeypatch):
    """Binaries installed before digests were recorded must be re-verified,
    not grandfathered in on trust."""
    bin_dir = tmp_path / "syncthing"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "syncthing"
    binary.write_bytes(b"unknown provenance")

    monkeypatch.setattr(config, "SYNCTHING_BIN_DIR", bin_dir)
    monkeypatch.setattr(config, "syncthing_binary_path", lambda: binary)
    monkeypatch.setattr(syncthing, "_binary_version", lambda _b: "v2.0.16")

    hit = {"n": 0}

    def fake_download(_url):
        hit["n"] += 1
        raise syncthing.URLError("offline")

    monkeypatch.setattr(syncthing, "_download", fake_download)

    with pytest.raises(SyncthingError):
        syncthing.ensure_binary("v2.0.16")
    assert hit["n"] == 1


def test_record_binary_digest_roundtrip(tmp_path, monkeypatch):
    bin_dir = tmp_path / "syncthing"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "syncthing"
    binary.write_bytes(b"payload")
    monkeypatch.setattr(config, "SYNCTHING_BIN_DIR", bin_dir)

    syncthing._record_binary_digest(binary, "v2.0.16")
    assert syncthing._binary_digest_matches(binary, "v2.0.16")

    binary.write_bytes(b"payload tampered")
    assert not syncthing._binary_digest_matches(binary, "v2.0.16")


def test_binary_digest_rejects_version_mismatch(tmp_path, monkeypatch):
    bin_dir = tmp_path / "syncthing"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "syncthing"
    binary.write_bytes(b"payload")
    monkeypatch.setattr(config, "SYNCTHING_BIN_DIR", bin_dir)

    syncthing._record_binary_digest(binary, "v2.0.16")
    assert not syncthing._binary_digest_matches(binary, "v2.0.17")


# ---------------------------------------------------------------------------
# Changing the sync folder must repoint every consumer of it.
# ---------------------------------------------------------------------------


def test_folder_change_restarts_syncthing_and_file_transfer(tmp_path, monkeypatch):
    """Syncthing reads the folder path once, at prepare_home() time, and
    FileTransfer schedules its observer at construction. Restarting only
    ClipboardSync left both on the old folder, so clipboard.txt was written
    where no peer replicated it and sync silently stopped."""
    from clipsync import main as main_mod

    app = object.__new__(main_mod.ClipSyncApp)
    app.settings = config.Settings(path=tmp_path / "settings.json")

    events: list[str] = []

    class _Stub:
        def __init__(self, name):
            self._name = name

        def stop(self):
            events.append(f"{self._name}.stop")

        def start(self):
            events.append(f"{self._name}.start")

    app.clipboard = _Stub("clipboard")
    app.file_transfer = _Stub("file_transfer")
    app.syncthing = _Stub("syncthing")
    app._start_syncthing_with_retry = lambda: events.append("syncthing.start")
    app._on_file_received = lambda *_a: None

    monkeypatch.setattr(main_mod, "ClipboardSync", lambda _s: _Stub("clipboard"))
    monkeypatch.setattr(main_mod, "FileTransfer", lambda _s, on_received=None: _Stub("file_transfer"))

    new_folder = tmp_path / "new_sync"
    app._on_folder_changed(str(new_folder))

    assert new_folder.exists()
    assert "syncthing.stop" in events and "syncthing.start" in events, (
        "Syncthing was not restarted, so it still replicates the old folder"
    )
    assert "file_transfer.stop" in events and "file_transfer.start" in events, (
        "FileTransfer still watches the old directory"
    )
    assert events.index("syncthing.start") < events.index("clipboard.start"), (
        "clipboard restarted before Syncthing was reconfigured"
    )
    # prepare_home() reads the folder back out of settings when it re-patches
    # config.xml, so the handler must not rely on the UI process having
    # persisted it first.
    assert app.settings.get("sync_folder") == str(new_folder)


# ---------------------------------------------------------------------------
# Concurrency: sets touched from more than one thread.
# ---------------------------------------------------------------------------


def test_file_transfer_delivers_each_file_once_under_concurrency(tmp_path):
    """watchdog dispatches from a thread pool on Windows, so an unguarded
    check-then-add on _seen let two events for one file both pass, delivering
    it twice."""
    import threading

    from clipsync import file_transfer as ft_mod

    delivered: list[Path] = []
    deliver_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def on_received(path, _sender):
        with deliver_lock:
            delivered.append(path)

    class _SlowSet(set):
        """The real check-then-add is two adjacent bytecodes, so the GIL
        almost never splits it and the race will not reproduce by chance.
        Sleeping inside the membership test widens the window to what a
        thread-pool dispatch can actually hit. Under a correct lock only one
        thread is ever inside this at a time, so the result is unchanged."""

        def __contains__(self, item):
            # Resolve membership FIRST, then sleep. Sleeping before the real
            # lookup would let the winner add the key while the others are
            # parked, so they would see it present and the race would hide
            # itself. This ordering parks every thread on the same "not
            # present" answer, which is the interleaving being guarded.
            result = super().__contains__(item)
            time.sleep(0.005)
            return result

    handler = ft_mod._FileReceiveHandler(on_received=on_received)
    handler._seen = _SlowSet()
    incoming = tmp_path / "files" / "peer-host" / "report.pdf"
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(b"data")

    def fire():
        barrier.wait()
        handler._handle(incoming)

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads)
    assert len(delivered) == 1, f"file delivered {len(delivered)} times; _seen check-then-add is not atomic"


def test_debounce_dict_does_not_grow_unbounded(tmp_path, monkeypatch):
    """Per-path deadlines are keyed by filename, and only two filenames ever
    match, so the dict cannot grow with event volume."""
    sync = _make_sync(tmp_path)
    handler = _ClipboardFileHandler(sync)
    monkeypatch.setattr(handler, "_matches", lambda _p: True)

    for i in range(500):
        handler._dispatch(str(sync.clipboard_file))
        handler._dispatch(str(sync.clipboard_image_file))
        if i % 100 == 0:
            handler._debounce_until = {k: v - 1.0 for k, v in handler._debounce_until.items()}

    assert len(handler._debounce_until) <= 2, f"debounce map grew to {len(handler._debounce_until)} entries"
