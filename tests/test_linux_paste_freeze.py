"""Regression tests for the Linux paste-freeze bug.

Root cause: _out_tick called _read_clipboard_image() on every 0.5 s poll tick.
On Linux that spawns xclip which sends an X11 SelectionRequest to the clipboard
owner (typically a browser).  Browsers service these on their main thread, so
hammering at 0.5 s caused the browser to freeze when the user tried to paste.

The fix has two parts:
  1. _read_image_from_system_clipboard() now runs a cheap TARGETS/list-types
     pre-check and only fetches image bytes when the clipboard actually
     advertises image/png.  This is enforced even during the IN-loop
     self-originated check so we never send image/png SelectionRequests when
     the clipboard holds text.
  2. _out_tick() rate-limits image checks on Linux to _LINUX_IMAGE_CHECK_INTERVAL
     seconds (default 2 s) instead of every poll tick.
"""

from __future__ import annotations

import io
import subprocess
import sys
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
    _LINUX_IMAGE_CHECK_INTERVAL,
    _linux_clipboard_has_image,
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
# _linux_clipboard_has_image unit tests
# ---------------------------------------------------------------------------


class TestLinuxClipboardHasImage:
    """Unit tests for the TARGETS pre-check helper."""

    def _run(self, cmd, **kwargs):
        """Default subprocess.run stub: xclip not found, wl-paste not found."""
        raise FileNotFoundError

    def test_returns_false_when_no_tools_installed(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", self._run)
        assert _linux_clipboard_has_image() is False

    def test_returns_false_when_xclip_targets_has_no_image(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if "TARGETS" in cmd or "--list-types" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"UTF8_STRING\ntext/plain\nTARGETS\n"
                return result
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _linux_clipboard_has_image() is False

    def test_returns_true_when_xclip_targets_includes_image_png(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if "TARGETS" in cmd:
                result = mock.Mock()
                result.returncode = 0
                result.stdout = b"UTF8_STRING\nimage/png\ntext/plain\n"
                return result
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _linux_clipboard_has_image() is True

    def test_falls_back_to_wl_paste_when_xclip_missing(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "xclip":
                raise FileNotFoundError
            # wl-paste --list-types
            result = mock.Mock()
            result.returncode = 0
            result.stdout = b"image/png\ntext/plain\n"
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _linux_clipboard_has_image() is True

    def test_returns_false_when_xclip_exits_nonzero(self, monkeypatch):
        """A non-zero exit from TARGETS means the clipboard is empty or an error
        occurred — in both cases there is no image."""

        def fake_run(cmd, **kwargs):
            result = mock.Mock()
            result.returncode = 1
            result.stdout = b""
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _linux_clipboard_has_image() is False

    def test_skips_timed_out_tool_continues_to_next(self, monkeypatch):
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == "xclip":
                raise subprocess.TimeoutExpired(cmd, 1)
            # wl-paste returns image/png
            result = mock.Mock()
            result.returncode = 0
            result.stdout = b"image/png\n"
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = _linux_clipboard_has_image()
        assert "xclip" in calls
        assert "wl-paste" in calls
        assert result is True


# ---------------------------------------------------------------------------
# _read_image_from_system_clipboard: TARGETS gate on Linux
# ---------------------------------------------------------------------------


class TestReadImageGate:
    """The TARGETS pre-check must prevent image/png requests when no image present."""

    @pytest.fixture(autouse=True)
    def _force_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

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
# _out_tick: rate-limiting on Linux
# ---------------------------------------------------------------------------


class TestOutTickRateLimiting:
    """Image checks in _out_tick must be throttled on Linux."""

    @pytest.fixture(autouse=True)
    def _force_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def _make_sync(self, tmp_path) -> ClipboardSync:
        settings = config.Settings(path=tmp_path / "settings.json")
        settings.set("sync_folder", str(tmp_path / "sync"))
        (tmp_path / "sync").mkdir()
        sync = ClipboardSync(settings)
        sync._read_clipboard = lambda: "hello"  # type: ignore[method-assign]
        sync._write_clipboard = lambda v: True  # type: ignore[method-assign]
        return sync

    def test_image_check_skipped_when_recently_checked(self, tmp_path):
        """If _last_image_check was just set, _read_clipboard_image must not be called."""
        sync = self._make_sync(tmp_path)
        image_check_count = {"n": 0}

        def counting_image_read():
            image_check_count["n"] += 1
            return None

        sync._read_clipboard_image = counting_image_read  # type: ignore[method-assign]
        sync._last_image_check = time.monotonic()  # pretend we just checked

        sync._out_tick()

        assert image_check_count["n"] == 0, (
            f"Image check ran despite throttle; called {image_check_count['n']} time(s)"
        )

    def test_image_check_runs_when_interval_elapsed(self, tmp_path, monkeypatch):
        """After _LINUX_IMAGE_CHECK_INTERVAL has elapsed, the check must fire."""
        monkeypatch.setattr(clipboard_module, "_LINUX_IMAGE_CHECK_INTERVAL", 0.0)
        sync = self._make_sync(tmp_path)
        image_check_count = {"n": 0}

        def counting_image_read():
            image_check_count["n"] += 1
            return None

        sync._read_clipboard_image = counting_image_read  # type: ignore[method-assign]
        sync._last_image_check = 0.0  # never checked

        sync._out_tick()

        assert image_check_count["n"] == 1, (
            f"Expected 1 image check but got {image_check_count['n']}"
        )

    def test_text_sync_still_works_when_image_check_throttled(self, tmp_path, monkeypatch):
        """Text clipboard sync must not be blocked by the image throttle."""
        sync = self._make_sync(tmp_path)
        sync._last_image_check = time.monotonic()  # throttle image check
        sync._last_synced = None  # force a text sync
        write_count = {"n": 0}

        original_write_file = sync._write_file

        def counting_write(text: str) -> None:
            write_count["n"] += 1
            # Don't actually write to disk in this unit test
            pass

        sync._write_file = counting_write  # type: ignore[method-assign]
        sync._out_tick()

        assert write_count["n"] == 1, (
            f"Expected text to be written once but got {write_count['n']} write(s)"
        )

    def test_image_check_interval_is_at_least_two_seconds(self):
        """Guard against accidentally reducing the interval back to 0.5 s."""
        assert _LINUX_IMAGE_CHECK_INTERVAL >= 2.0, (
            f"_LINUX_IMAGE_CHECK_INTERVAL={_LINUX_IMAGE_CHECK_INTERVAL} is too small; "
            "values < 2 s risk reintroducing the paste-freeze bug"
        )


# ---------------------------------------------------------------------------
# End-to-end: image sync still works with throttling enabled
# ---------------------------------------------------------------------------


@pytest.fixture
def two_sided_linux(tmp_path, monkeypatch):
    """Two ClipboardSync instances on a shared folder, image interval shrunk for speed."""
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    monkeypatch.setattr(clipboard_module, "_LINUX_IMAGE_CHECK_INTERVAL", POLL * 2)
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


def test_image_syncs_with_linux_throttle(two_sided_linux) -> None:
    """Images must still reach the remote peer even when the Linux throttle is active."""
    _, _, _, img_a, _, img_b = two_sided_linux
    img_a["image"] = FAKE_PNG
    assert _wait_for(lambda: img_b["image"] == FAKE_PNG, timeout=5.0), (
        f"Image did not sync under Linux throttle; got {img_b['image']!r}"
    )


def test_text_syncs_normally_under_linux_throttle(two_sided_linux) -> None:
    """Text sync must not be disrupted by the image-check rate limiter."""
    _, _, txt_a, _, txt_b, _ = two_sided_linux
    txt_a["value"] = "paste freeze fixed"
    assert _wait_for(lambda: txt_b["value"] == "paste freeze fixed"), (
        f"Text did not sync; got {txt_b['value']!r}"
    )


def test_image_polling_frequency_bounded_on_linux(two_sided_linux, monkeypatch) -> None:
    """The image read must not be called more often than _LINUX_IMAGE_CHECK_INTERVAL."""
    a, _, _, _, _, _ = two_sided_linux
    image_check_times: list[float] = []
    original = a._read_clipboard_image

    def tracking_read():
        image_check_times.append(time.monotonic())
        return original()

    a._read_clipboard_image = tracking_read  # type: ignore[method-assign]

    observe_duration = POLL * 30  # ~1.5 s at POLL=0.05
    time.sleep(observe_duration)

    if len(image_check_times) >= 2:
        gaps = [image_check_times[i + 1] - image_check_times[i] for i in range(len(image_check_times) - 1)]
        min_gap = min(gaps)
        # Allow 20 % tolerance below the configured interval.
        threshold = clipboard_module._LINUX_IMAGE_CHECK_INTERVAL * 0.8
        assert min_gap >= threshold, (
            f"Image check fired too frequently: min gap {min_gap:.3f}s < threshold {threshold:.3f}s. "
            f"Gaps: {[f'{g:.3f}' for g in gaps]}"
        )
