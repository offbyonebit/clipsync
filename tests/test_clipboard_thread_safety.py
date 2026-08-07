"""The native clipboard is one shared object and is not thread-safe.

The OUT loop reads it while the IN loop writes it, from two different threads.
On macOS pyperclip's PyObjC backend drives NSPasteboard directly, and a read
overlapping a write crashes the process inside
-[_NSPasteboardOwnersCollection handleOwnershipChange] (SIGSEGV), taking the
tray down with it. These tests fail if the serializing lock is ever dropped.

The instrumented clipboard calls sleep rather than spin: sleeping releases the
GIL, so an unsynchronized second thread reliably lands inside the window. A
busy loop does not yield often enough to expose the race.
"""

from __future__ import annotations

import threading
import time

import pytest

from clipsync import clipboard as clipboard_mod
from clipsync import config
from clipsync.clipboard import ClipboardSync

# Long enough to force a GIL handoff, short enough to keep the suite fast.
_WINDOW = 0.002


@pytest.fixture(autouse=True)
def _isolate_history(tmp_path, monkeypatch):
    """ClipboardHistory binds to config.HISTORY_FILE at construction. Without
    this the tests would read and rewrite the real clipboard history."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")


def _make_sync(tmp_path):
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    settings = config.Settings(path=tmp_path / "settings.json")
    settings.set("sync_folder", str(sync_folder))
    return ClipboardSync(settings)


class _OverlapDetector:
    """Records whether two instrumented clipboard calls were ever in flight at
    the same time."""

    def __init__(self) -> None:
        self.inside = 0
        self.overlapped = False
        self._guard = threading.Lock()

    def instrument(self, result):
        def fn(*_args, **_kwargs):
            with self._guard:
                self.inside += 1
                if self.inside > 1:
                    self.overlapped = True
            try:
                time.sleep(_WINDOW)
                return result
            finally:
                with self._guard:
                    self.inside -= 1

        return fn


def _run(targets, timeout=30):
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert not any(t.is_alive() for t in threads), "clipboard access deadlocked"


def test_clipboard_reads_and_writes_never_overlap(tmp_path, monkeypatch):
    sync = _make_sync(tmp_path)
    detector = _OverlapDetector()

    monkeypatch.setattr(clipboard_mod.pyperclip, "paste", detector.instrument("text"))
    monkeypatch.setattr(clipboard_mod.pyperclip, "copy", detector.instrument(None))
    # The Linux in-process owner would bypass the pyperclip write path.
    sync._clipboard_owner = None

    _run(
        [
            lambda: [sync._read_clipboard() for _ in range(40)],
            lambda: [sync._write_clipboard("value") for _ in range(40)],
        ]
    )

    assert not detector.overlapped, (
        "a clipboard read overlapped a write; the native clipboard is not thread-safe and this segfaults on macOS"
    )


def test_image_clipboard_access_is_serialized_with_text(tmp_path, monkeypatch):
    """Image and text paths touch the same pasteboard, so they must share one
    lock rather than each holding a private one."""
    sync = _make_sync(tmp_path)
    detector = _OverlapDetector()

    monkeypatch.setattr(clipboard_mod.pyperclip, "paste", detector.instrument("text"))
    monkeypatch.setattr(clipboard_mod, "_read_image_from_system_clipboard", detector.instrument(None))
    monkeypatch.setattr(clipboard_mod, "_write_image_to_system_clipboard", detector.instrument(True))

    _run(
        [
            lambda: [sync._read_clipboard() for _ in range(30)],
            lambda: [sync._read_clipboard_image() for _ in range(30)],
            lambda: [sync._write_clipboard_image(b"png") for _ in range(30)],
        ]
    )

    assert not detector.overlapped, "image and text clipboard access must share one lock"
