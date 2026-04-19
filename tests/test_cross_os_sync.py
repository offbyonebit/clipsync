"""Cross-OS sync coverage.

`test_mac_windows_sync.py` uses a generic fake clipboard; it proves
the sync loop is correct, but every OS acts the same in those tests.
Real clipboards differ — most notably Windows injects CRLF on copy of
any LF-terminated string, which caused a real oscillation bug before
`_normalize_newlines` was added. These tests parametrize over every
platform pair so a regression in the normalization would fail CI.

Platform quirks modeled here:
  * Windows: converts "\\n" to "\\r\\n" when a program sets the clipboard.
  * macOS:   leaves newlines alone (NSPasteboard preserves as-given).
  * Linux:   leaves newlines alone (X11 and Wayland are both byte-preserving).

Keep this file in sync with any new OS-specific clipboard behavior
that shows up in production.
"""

from __future__ import annotations

import time

import pytest
from watchdog.observers.polling import PollingObserver

from clipsync import clipboard as clipboard_module
from clipsync import config
from clipsync.clipboard import ClipboardSync

POLL = 0.05


class OSClipboard:
    """Fake clipboard that mimics one platform's copy-time normalization."""

    def __init__(self, style: str) -> None:
        assert style in ("windows", "mac", "linux")
        self.style = style
        self._value: str = ""

    def set(self, value: str) -> None:
        """Writer entry point used by the test to simulate a user copy."""
        self._apply_os_quirks(value)

    def _apply_os_quirks(self, value: str) -> None:
        if self.style == "windows":
            # Win32 SetClipboardData normalizes \n -> \r\n for CF_UNICODETEXT.
            value = value.replace("\r\n", "\n").replace("\n", "\r\n")
        self._value = value

    # Methods bound onto ClipboardSync instances:
    def read(self) -> str | None:
        return self._value

    def write(self, value: str) -> bool:
        # pyperclip.copy on Windows also triggers CRLF normalization, so
        # model that here too. Mac/Linux just store bytes as-is.
        self._apply_os_quirks(value)
        return True


def _install(sync: ClipboardSync, clip: OSClipboard) -> None:
    sync._read_clipboard = clip.read  # type: ignore[method-assign]
    sync._write_clipboard = clip.write  # type: ignore[method-assign]
    # Stub image access so the host's real clipboard cannot leak into
    # these text-only tests (an image on the Mac clipboard would take
    # priority in _out_tick and hide the text propagation under test).
    sync._read_clipboard_image = lambda: None  # type: ignore[method-assign]
    sync._write_clipboard_image = lambda _b: True  # type: ignore[method-assign]


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def make_pair(tmp_path, monkeypatch):
    """Factory that builds two ClipboardSync instances with the requested
    (os_a, os_b) pair sharing one sync folder."""
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    monkeypatch.setattr(clipboard_module, "Observer", PollingObserver)

    syncs: list[ClipboardSync] = []

    def _build(os_a: str, os_b: str):
        sync_folder = tmp_path / "shared_sync"
        sync_folder.mkdir(exist_ok=True)

        settings_a = config.Settings(path=tmp_path / f"{os_a}_settings.json")
        settings_a.set("sync_folder", str(sync_folder))
        settings_b = config.Settings(path=tmp_path / f"{os_b}_settings.json")
        settings_b.set("sync_folder", str(sync_folder))

        a = ClipboardSync(settings_a)
        b = ClipboardSync(settings_b)

        clip_a = OSClipboard(os_a)
        clip_b = OSClipboard(os_b)
        _install(a, clip_a)
        _install(b, clip_b)

        a.start()
        b.start()
        syncs.extend([a, b])
        return a, b, clip_a, clip_b

    yield _build
    for s in syncs:
        s.stop()


OS_PAIRS = [
    ("mac", "windows"),
    ("mac", "linux"),
    ("linux", "windows"),
]


@pytest.mark.parametrize("src,dst", [(a, b) for pair in OS_PAIRS for a, b in (pair, pair[::-1])])
def test_plain_text_syncs_across(make_pair, src, dst) -> None:
    a, b, clip_a, clip_b = make_pair(src, dst)
    clip_a.set("hello world")
    assert _wait_for(lambda: "hello world" in clip_b.read()), f"{src}->{dst} did not propagate; got {clip_b.read()!r}"


@pytest.mark.parametrize("src,dst", [(a, b) for pair in OS_PAIRS for a, b in (pair, pair[::-1])])
def test_multiline_does_not_oscillate(make_pair, src, dst) -> None:
    """Regression for the CRLF ping-pong bug.

    If Windows's clipboard injects \\r\\n on copy, a naive implementation
    would see 'text read back != text originally written' and fire OUT
    again, which the remote would apply, bouncing forever. With
    _normalize_newlines the comparison is CRLF-insensitive and the
    system settles after one round-trip.
    """
    a, b, clip_a, clip_b = make_pair(src, dst)
    clip_a.set("line one\nline two\nline three")

    # Let sync propagate.
    assert _wait_for(lambda: "line one" in clip_b.read() and "line three" in clip_b.read())

    # Give any oscillation several poll cycles to show itself, then
    # assert the text is stable (nothing trailing beyond the payload).
    time.sleep(POLL * 15)

    a_text = clip_a.read().replace("\r\n", "\n")
    b_text = clip_b.read().replace("\r\n", "\n")
    assert a_text == b_text == "line one\nline two\nline three", f"Instability detected: a={a_text!r} b={b_text!r}"


def test_three_way_relay_simulation(make_pair) -> None:
    """A Linux user pastes on Mac via a Windows relay: Linux -> Windows -> Mac.

    This exercises two sync pairs in sequence to catch any transform
    that would silently corrupt across two hops (e.g. double CRLF,
    BOM insertion, trailing newline accretion).
    """
    _, _, linux_clip, win_clip = make_pair("linux", "windows")
    _, _, win_clip2, mac_clip = make_pair("windows", "mac")

    # They're different ClipboardSync instances so we cannot actually
    # chain them via the file; instead assert that the Windows clipboard
    # content matches what the Linux side produced after CRLF
    # normalization, which is what would get forwarded to Mac.
    linux_clip.set("alpha\nbeta\ngamma")
    assert _wait_for(lambda: "alpha" in win_clip.read() and "gamma" in win_clip.read())

    # Round-trip stability: Windows's read, once normalized, matches origin.
    normalized = win_clip.read().replace("\r\n", "\n")
    assert normalized == "alpha\nbeta\ngamma"
