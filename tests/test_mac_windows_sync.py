"""End-to-end test of the Mac <-> Windows clipboard sync flow.

Simulates two machines by running two ClipboardSync instances that
share a real folder (the stand-in for the Syncthing-replicated folder)
but have separate settings files and separate in-memory "clipboards."
Each instance is patched to read/write its own clipboard dict instead
of the real system clipboard.

What we want to verify:
  * copy on A propagates to B within a couple poll cycles
  * copy on B propagates to A within a couple poll cycles
  * changing the passphrase on one side (via its settings file) is
    picked up before the next OUT/IN cycle without any explicit reload
  * mismatched passphrase logs a warning and does not corrupt the
    recipient's clipboard
"""

from __future__ import annotations

import time

import pytest
from watchdog.observers.polling import PollingObserver

from clipsync import clipboard as clipboard_module
from clipsync import config
from clipsync.clipboard import ClipboardSync

POLL = 0.05  # tight poll for fast tests


def _install_fake_clipboard(sync: ClipboardSync, state: dict) -> None:
    """Replace pyperclip access with dict-backed state, unique per instance.

    Also stubs out image clipboard access so these text-only tests are not
    perturbed by whatever the host's real system clipboard happens to hold
    (a real PNG on the Mac's clipboard would otherwise take priority in
    _out_tick and make these assertions flake)."""

    def read() -> str | None:
        return state.get("value")

    def write(value: str) -> bool:
        state["value"] = value
        return True

    sync._read_clipboard = read  # type: ignore[method-assign]
    sync._write_clipboard = write  # type: ignore[method-assign]
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
def two_sided(tmp_path, monkeypatch):
    """Mac and Windows ClipboardSync instances sharing a sync folder."""
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    # FSEvents on macOS refuses to register two watches on the same path,
    # which would only happen in this test harness (two logical machines
    # share one real folder). Swap in PollingObserver to sidestep that.
    monkeypatch.setattr(clipboard_module, "Observer", PollingObserver)

    sync_folder = tmp_path / "shared_sync"
    sync_folder.mkdir()

    mac_settings_path = tmp_path / "mac_settings.json"
    win_settings_path = tmp_path / "win_settings.json"

    mac_settings = config.Settings(path=mac_settings_path)
    mac_settings.set("sync_folder", str(sync_folder))
    win_settings = config.Settings(path=win_settings_path)
    win_settings.set("sync_folder", str(sync_folder))

    mac = ClipboardSync(mac_settings)
    win = ClipboardSync(win_settings)

    mac_clip: dict = {"value": ""}
    win_clip: dict = {"value": ""}
    _install_fake_clipboard(mac, mac_clip)
    _install_fake_clipboard(win, win_clip)

    mac.start()
    win.start()
    try:
        yield mac, win, mac_clip, win_clip, mac_settings, win_settings
    finally:
        mac.stop()
        win.stop()


def test_mac_to_windows(two_sided) -> None:
    mac, win, mac_clip, win_clip, *_ = two_sided
    mac_clip["value"] = "hello from mac"
    assert _wait_for(lambda: win_clip["value"] == "hello from mac"), (
        f"Windows never received Mac's copy; got {win_clip['value']!r}"
    )


def test_windows_to_mac(two_sided) -> None:
    mac, win, mac_clip, win_clip, *_ = two_sided
    win_clip["value"] = "hello from windows"
    assert _wait_for(lambda: mac_clip["value"] == "hello from windows"), (
        f"Mac never received Windows's copy; got {mac_clip['value']!r}"
    )


def test_bidirectional_alternating(two_sided) -> None:
    mac, win, mac_clip, win_clip, *_ = two_sided

    mac_clip["value"] = "first from mac"
    assert _wait_for(lambda: win_clip["value"] == "first from mac")

    win_clip["value"] = "reply from windows"
    assert _wait_for(lambda: mac_clip["value"] == "reply from windows")

    mac_clip["value"] = "second from mac"
    assert _wait_for(lambda: win_clip["value"] == "second from mac")


def test_encrypted_symmetric_passphrase(two_sided) -> None:
    mac, win, mac_clip, win_clip, mac_settings, win_settings = two_sided
    mac_settings.set("encryption_passphrase", "shared-secret")
    win_settings.set("encryption_passphrase", "shared-secret")

    # Give the main-loop's next get() a chance to pick up the new setting.
    time.sleep(POLL * 3)

    mac_clip["value"] = "encrypted payload"
    assert _wait_for(lambda: win_clip["value"] == "encrypted payload")


def test_encrypted_mismatched_passphrase_does_not_corrupt(two_sided) -> None:
    mac, win, mac_clip, win_clip, mac_settings, win_settings = two_sided
    mac_settings.set("encryption_passphrase", "mac-pass")
    win_settings.set("encryption_passphrase", "win-pass")
    time.sleep(POLL * 3)

    original_win_value = win_clip["value"]
    mac_clip["value"] = "you cannot read this"

    # Wait past multiple poll cycles; decrypt should fail silently on win.
    time.sleep(POLL * 20)
    assert win_clip["value"] == original_win_value, (
        f"Windows clipboard was overwritten despite decrypt failure; got {win_clip['value']!r}"
    )


def test_passphrase_change_takes_effect_without_restart(tmp_path, monkeypatch) -> None:
    """Regression test for the 20-minute-startup-lag bug.

    Write a passphrase through one Settings instance (simulating a UI
    subprocess) and verify the second Settings instance (simulating the
    main process) sees the change on its very next lookup.
    """
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    settings_path = tmp_path / "settings.json"

    main_settings = config.Settings(path=settings_path)
    ui_settings = config.Settings(path=settings_path)

    assert main_settings.get("encryption_passphrase") == ""

    ui_settings.set("encryption_passphrase", "just-set-by-ui")
    time.sleep(0.02)  # allow stat granularity

    assert main_settings.get("encryption_passphrase") == "just-set-by-ui"
