"""Regression tests for the Linux paste-freeze bug.

Root cause: the OUT loop used to poll the clipboard every 0.5 s, and image
checks ran on every poll tick.  On Linux that spawns xclip which sends an X11
SelectionRequest to the clipboard owner (typically a browser).  Browsers
service these on their main thread, so hammering at 0.5 s caused the browser
to freeze when the user tried to paste.

The fix has two parts:
  1. _read_image_from_system_clipboard() runs a cheap TARGETS/list-types
     pre-check and only fetches image bytes when the clipboard actually
     advertises image/png, so no image/png SelectionRequest is ever sent
     when the clipboard holds text.
  2. The OUT loop no longer polls on Linux: a clipboard-owner watcher built on
     the X11 XFIXES extension wakes _out_tick() only when the CLIPBOARD
     selection owner actually changes (i.e. the user copied something), with
     a 300 ms debounce so we don't compete with an immediate paste in the same
     gesture.  xclip/wl-paste is therefore invoked on real clipboard changes
     only, never on a fixed interval.  Wayland and environments without
     XFIXES fall back to polling at CLIPBOARD_POLL_INTERVAL, same as before.
"""

from __future__ import annotations

import io
import queue
import subprocess
import sys
import threading
import time
import types
import unittest.mock as mock

import pytest
from PIL import Image
from watchdog.observers.polling import PollingObserver

import clipsync.clipboard as clipboard_module
from clipsync import config
from clipsync.clipboard import (
    ClipboardSync,
    _STOP_SENTINEL,
    _read_image_from_system_clipboard,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
POLL = 0.05


def _make_png(color: tuple[int, int, int] = (0, 128, 255)) -> bytes:
    img = Image.new("RGB", (1, 1), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


FAKE_PNG = _make_png()


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# _read_image_from_system_clipboard: TARGETS gate on Linux
#
# The TARGETS pre-check used to live in a separate _linux_clipboard_has_image()
# helper; it's now inlined directly into _read_image_from_system_clipboard(),
# so these tests exercise that function end-to-end instead of a standalone
# boolean helper.
# ---------------------------------------------------------------------------


class TestReadImageGate:
    """The TARGETS pre-check must prevent image/png requests when no image present."""

    @pytest.fixture(autouse=True)
    def _force_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def test_returns_none_when_no_tools_installed_at_all(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        assert _read_image_from_system_clipboard() is None

    def test_falls_back_to_wl_paste_when_xclip_missing(self, monkeypatch):
        """If xclip isn't installed, the TARGETS check must still try wl-paste."""

        def fake_run(cmd, **kwargs):
            if cmd[0] == "xclip":
                raise FileNotFoundError
            if "--list-types" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"image/png\ntext/plain\n"
                return result
            if "image/png" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = FAKE_PNG
                return result
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _read_image_from_system_clipboard() == FAKE_PNG

    def test_returns_none_when_targets_exits_nonzero(self, monkeypatch):
        """A non-zero exit from TARGETS means the clipboard is empty or an error
        occurred — in both cases there is no image."""

        def fake_run(cmd, **kwargs):
            result = mock.Mock()
            result.returncode = 1
            result.stdout = b""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _read_image_from_system_clipboard() is None

    def test_skips_timed_out_tool_continues_to_next(self, monkeypatch):
        """If xclip's TARGETS check times out, wl-paste must still be tried."""
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == "xclip":
                raise subprocess.TimeoutExpired(cmd, 1)
            if "--list-types" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"image/png\n"
                return result
            result = mock.Mock()
            result.returncode = 0
            result.stdout = FAKE_PNG
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _read_image_from_system_clipboard()
        assert "xclip" in calls
        assert "wl-paste" in calls
        assert result == FAKE_PNG

    def test_skips_image_fetch_when_targets_has_no_image(self, monkeypatch):
        """When TARGETS returns no image/png the image/png fetch must not run."""
        image_fetch_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if "TARGETS" in cmd or "--list-types" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"UTF8_STRING\ntext/plain\n"
                return result
            # If we reach here, an image/png fetch was made — that's the bug.
            image_fetch_calls.append(cmd)
            result = mock.Mock()
            result.returncode = 1
            result.stdout = b""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _read_image_from_system_clipboard()
        assert result is None
        assert image_fetch_calls == [], (
            f"image/png SelectionRequest was sent despite no image in TARGETS: {image_fetch_calls}"
        )

    def test_fetches_image_bytes_when_targets_has_image_png(self, monkeypatch):
        """When TARGETS advertises image/png, we must proceed to fetch the bytes."""

        def fake_run(cmd, **kwargs):
            if "TARGETS" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"image/png\nUTF8_STRING\n"
                return result
            if "image/png" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = FAKE_PNG
                return result
            raise AssertionError(f"Unexpected: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _read_image_from_system_clipboard()
        assert result == FAKE_PNG

    def test_returns_none_when_no_tools_installed(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        assert _read_image_from_system_clipboard() is None


# ---------------------------------------------------------------------------
# _out_loop: XFixes-event-driven dispatch on Linux
#
# There is no interval-based throttle anymore. Instead, when an XFixes
# watcher is available, _out_loop blocks on _xfixes_queue and only calls
# _out_tick() when the CLIPBOARD selection owner actually changes, after a
# 300 ms debounce. These tests drive _out_loop directly with a fake queue
# standing in for the real XFixes watcher thread.
# ---------------------------------------------------------------------------


class TestOutLoopEventDriven:
    """_out_tick must run only on real clipboard-owner-change events, debounced."""

    @pytest.fixture(autouse=True)
    def _force_linux(self, tmp_path, monkeypatch):
        # tmp_path must be resolved before the platform patch: pytest's own
        # tmp_path fixture checks sys.platform to decide whether to call
        # os.getuid(), which doesn't exist on real Windows.
        monkeypatch.setattr(sys, "platform", "linux")

    def _make_sync(self, tmp_path) -> ClipboardSync:
        settings = config.Settings(path=tmp_path / "settings.json")
        settings.set("sync_folder", str(tmp_path / "sync"))
        (tmp_path / "sync").mkdir()
        sync = ClipboardSync(settings)
        sync._read_clipboard = lambda: None  # type: ignore[method-assign]
        sync._read_clipboard_image = lambda: None  # type: ignore[method-assign]
        sync._write_clipboard = lambda v: True  # type: ignore[method-assign]
        return sync

    def _run_loop(self, sync: ClipboardSync) -> threading.Thread:
        sync._xfixes_queue = queue.SimpleQueue()
        t = threading.Thread(target=sync._out_loop, daemon=True)
        t.start()
        return t

    def _stop_loop(self, sync: ClipboardSync, t: threading.Thread) -> None:
        sync._stop.set()
        sync._xfixes_queue.put(_STOP_SENTINEL)
        t.join(timeout=2.0)
        assert not t.is_alive(), "_out_loop did not exit after stop sentinel"

    def test_out_tick_runs_once_at_startup_with_no_events(self, tmp_path):
        """Only the initial tick fires; the loop must not poll while idle."""
        sync = self._make_sync(tmp_path)
        tick_count = {"n": 0}
        sync._out_tick = lambda: tick_count.__setitem__("n", tick_count["n"] + 1)  # type: ignore[method-assign]

        t = self._run_loop(sync)
        try:
            time.sleep(0.5)  # well past the 300 ms debounce window, no events sent
            assert tick_count["n"] == 1, f"Expected only the initial tick, got {tick_count['n']}"
        finally:
            self._stop_loop(sync, t)

    def test_out_tick_runs_after_xfixes_event_with_debounce(self, tmp_path):
        """An event must trigger a tick, but only after the ~300 ms debounce."""
        sync = self._make_sync(tmp_path)
        tick_times: list[float] = []
        sync._out_tick = lambda: tick_times.append(time.monotonic())  # type: ignore[method-assign]

        t = self._run_loop(sync)
        try:
            time.sleep(0.05)  # let the initial tick land
            event_time = time.monotonic()
            sync._xfixes_queue.put(True)
            assert _wait_for(lambda: len(tick_times) >= 2, timeout=2.0), "second tick never fired"
            gap = tick_times[1] - event_time
            assert gap >= 0.25, f"Tick fired too soon after event (after {gap:.3f}s); debounce not honoured"
        finally:
            self._stop_loop(sync, t)

    def test_rapid_events_during_debounce_collapse_to_one_tick(self, tmp_path):
        """Multiple events arriving within the debounce window must cause one tick, not one per event."""
        sync = self._make_sync(tmp_path)
        tick_count = {"n": 0}
        sync._out_tick = lambda: tick_count.__setitem__("n", tick_count["n"] + 1)  # type: ignore[method-assign]

        t = self._run_loop(sync)
        try:
            time.sleep(0.05)  # let the initial tick land
            for _ in range(5):
                sync._xfixes_queue.put(True)
                time.sleep(0.01)  # all within the 300 ms debounce window
            assert _wait_for(lambda: tick_count["n"] >= 2, timeout=2.0), "follow-up tick never fired"
            time.sleep(0.5)  # give any erroneous extra ticks time to show up
            assert tick_count["n"] == 2, f"Expected exactly 2 ticks (initial + 1 collapsed), got {tick_count['n']}"
        finally:
            self._stop_loop(sync, t)

    def test_stop_sentinel_exits_promptly_without_ticking(self, tmp_path):
        """Stopping while idle must not produce a spurious tick."""
        sync = self._make_sync(tmp_path)
        tick_count = {"n": 0}
        sync._out_tick = lambda: tick_count.__setitem__("n", tick_count["n"] + 1)  # type: ignore[method-assign]

        t = self._run_loop(sync)
        time.sleep(0.05)
        start = time.monotonic()
        self._stop_loop(sync, t)
        assert time.monotonic() - start < 1.0, "loop took too long to exit on stop sentinel"
        assert tick_count["n"] == 1, "only the initial tick should have fired"


# ---------------------------------------------------------------------------
# End-to-end: image sync still works on the Linux polling fallback
#
# These instances run start()/stop() directly; on a machine without a real
# X display (true in CI and on this dev box), _try_start_xfixes_watcher()
# fails to open the display and returns None, so ClipboardSync falls back to
# plain polling at CLIPBOARD_POLL_INTERVAL -- exercising the same path Wayland
# users hit.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_sided_linux(tmp_path, monkeypatch):
    """Two ClipboardSync instances on a shared folder, polling at a fast interval for speed."""
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    monkeypatch.setattr(clipboard_module, "Observer", PollingObserver)
    monkeypatch.setattr(sys, "platform", "linux")

    sync_folder = tmp_path / "shared_sync"
    sync_folder.mkdir()

    settings_a = config.Settings(path=tmp_path / "a_settings.json")
    settings_a.set("sync_folder", str(sync_folder))
    settings_b = config.Settings(path=tmp_path / "b_settings.json")
    settings_b.set("sync_folder", str(sync_folder))

    a = ClipboardSync(settings_a)
    b = ClipboardSync(settings_b)

    text_a: dict = {"value": ""}
    image_a: dict = {"image": None}
    text_b: dict = {"value": ""}
    image_b: dict = {"image": None}

    a._read_clipboard = lambda: text_a["value"] or None  # type: ignore[method-assign]
    a._write_clipboard = lambda v: text_a.__setitem__("value", v) or True  # type: ignore[method-assign]
    a._read_clipboard_image = lambda: image_a["image"]  # type: ignore[method-assign]
    a._write_clipboard_image = lambda b: image_a.__setitem__("image", b) or True  # type: ignore[method-assign]

    b._read_clipboard = lambda: text_b["value"] or None  # type: ignore[method-assign]
    b._write_clipboard = lambda v: text_b.__setitem__("value", v) or True  # type: ignore[method-assign]
    b._read_clipboard_image = lambda: image_b["image"]  # type: ignore[method-assign]
    b._write_clipboard_image = lambda bi: image_b.__setitem__("image", bi) or True  # type: ignore[method-assign]

    a.start()
    b.start()

    yield a, b, text_a, image_a, text_b, image_b

    a.stop()
    b.stop()


def test_image_syncs_on_linux_polling_fallback(two_sided_linux) -> None:
    """Images must still reach the remote peer when XFixes is unavailable."""
    _, _, _, img_a, _, img_b = two_sided_linux
    img_a["image"] = FAKE_PNG
    assert _wait_for(lambda: img_b["image"] == FAKE_PNG, timeout=5.0), (
        f"Image did not sync on polling fallback; got {img_b['image']!r}"
    )


def test_text_syncs_on_linux_polling_fallback(two_sided_linux) -> None:
    """Text sync must work normally alongside image checks on the polling path."""
    _, _, txt_a, _, txt_b, _ = two_sided_linux
    txt_a["value"] = "paste freeze fixed"
    assert _wait_for(lambda: txt_b["value"] == "paste freeze fixed"), (
        f"Text did not sync; got {txt_b['value']!r}"
    )
