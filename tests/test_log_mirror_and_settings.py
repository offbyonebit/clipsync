"""Log mirroring is opt-in, and malformed settings must not kill startup.

LogMirror copied this device's log into the shared folder every 10s. The
folder is replicated to every paired device, so the log (hostnames, device
IDs, file names, error traces) went to all of them. It was always on, had no
setting, and was not mentioned in the README. No clipboard text is ever
logged -- every clipboard log line records a character count -- but none of
that is visible from the tray.

Separately, ClipboardHistory parsed settings with a bare int(), and it is
constructed during startup, so one malformed value in settings.json took the
whole app down before the tray appeared.
"""

from __future__ import annotations

import pytest

from clipsync import config
from clipsync.debug import LogMirror
from clipsync.history import ClipboardHistory, _coerce_bool, _coerce_int


class _Settings:
    def __init__(self, **kw):
        self._d = dict(kw)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


@pytest.fixture
def synced(tmp_path, monkeypatch):
    log_file = tmp_path / "clipsync.log"
    log_file.write_text("2026-08-03 10:00:00 [INFO] clipsync.main: started\n")
    monkeypatch.setattr(config, "LOG_FILE", log_file)
    return tmp_path


def _mirror(synced, **kw):
    settings = _Settings(sync_folder=str(synced / "sync"), **kw)
    return LogMirror(settings), synced / "sync" / "debug"


# ---------------------------------------------------------------------------
# Mirroring is opt-in
# ---------------------------------------------------------------------------


def test_mirror_is_off_by_default(synced):
    """The default must not publish anything to the shared folder."""
    mirror, debug_dir = _mirror(synced)
    mirror._tick()
    assert not debug_dir.exists() or not list(debug_dir.glob("*.log")), (
        "log was mirrored into the shared folder without being asked"
    )


def test_mirror_publishes_when_explicitly_enabled(synced):
    mirror, debug_dir = _mirror(synced, debug_log_mirror=True)
    mirror._tick()
    published = list(debug_dir.glob("*.log"))
    assert published, "opting in did not publish the log"
    assert "started" in published[0].read_text()


def test_disabling_retracts_an_already_published_log(synced):
    """Switching it off has to remove what is already in the folder, or the
    last copy keeps being replicated to every peer indefinitely."""
    settings = _Settings(sync_folder=str(synced / "sync"), debug_log_mirror=True)
    mirror = LogMirror(settings)
    debug_dir = synced / "sync" / "debug"

    mirror._tick()
    assert list(debug_dir.glob("*.log"))

    settings.set("debug_log_mirror", False)
    mirror._tick()
    assert not list(debug_dir.glob("*.log")), "disabling left the published log behind"


def test_first_tick_cleans_up_a_log_left_by_an_older_build(synced):
    """Older builds always mirrored. After upgrading, the stale file must be
    retracted rather than left replicating forever."""
    debug_dir = synced / "sync" / "debug"
    debug_dir.mkdir(parents=True)
    from clipsync.debug import _safe_hostname

    stale = debug_dir / f"{_safe_hostname()}.log"
    stale.write_text("old always-on mirror output\n")

    mirror, _ = _mirror(synced)  # default: disabled
    mirror._tick()
    assert not stale.exists(), "stale mirror from an older build was not cleaned up"


def test_toggle_applies_without_restart(synced):
    settings = _Settings(sync_folder=str(synced / "sync"), debug_log_mirror=False)
    mirror = LogMirror(settings)
    debug_dir = synced / "sync" / "debug"

    mirror._tick()
    assert not list(debug_dir.glob("*.log"))
    settings.set("debug_log_mirror", True)
    mirror._tick()
    assert list(debug_dir.glob("*.log")), "enabling did not take effect until restart"


def test_peer_logs_are_never_removed(synced):
    """Retraction must only touch our own file."""
    settings = _Settings(sync_folder=str(synced / "sync"), debug_log_mirror=False)
    debug_dir = synced / "sync" / "debug"
    debug_dir.mkdir(parents=True)
    peer = debug_dir / "someone-elses-laptop.log"
    peer.write_text("peer output\n")

    LogMirror(settings)._tick()
    assert peer.exists(), "retraction deleted a peer's mirrored log"


# ---------------------------------------------------------------------------
# Settings coercion
# ---------------------------------------------------------------------------


def test_malformed_max_items_does_not_crash_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "h.json")
    h = ClipboardHistory(_Settings(history_max_items="not-a-number"))
    assert h.get_max_items() == 50, "bad value should fall back to the default"


def test_stringified_numbers_are_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "h.json")
    h = ClipboardHistory(_Settings(history_max_items="25"))
    assert h.get_max_items() == 25


def test_stringified_false_disables_history(tmp_path, monkeypatch):
    """bool("false") is True, which is exactly the trap a JSON-stringified
    setting falls into: history would stay on after the user turned it off."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "h.json")
    h = ClipboardHistory(_Settings(history_enabled="false"))
    assert h.is_enabled() is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10, 10),
        ("10", 10),
        (" 10 ", 10),
        (0, 5),
        (-3, 5),
        ("abc", 5),
        (None, 5),
        (True, 5),
        (3.7, 5),
    ],
)
def test_coerce_int(value, expected):
    assert _coerce_int(value, 5) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("nonsense", True),
        (None, True),
    ],
)
def test_coerce_bool(value, expected):
    assert _coerce_bool(value, True) is expected


def test_malformed_auto_clear_does_not_silently_keep_entries_forever(tmp_path, monkeypatch):
    """0 means never expire, so an unparseable value must fall back to 0 rather
    than to some arbitrary retention the user did not choose."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "h.json")
    h = ClipboardHistory(_Settings(history_auto_clear_minutes="oops"))
    assert h._auto_clear_minutes() == 0
    h2 = ClipboardHistory(_Settings(history_auto_clear_minutes="30"))
    assert h2._auto_clear_minutes() == 30


def test_history_still_works_with_sane_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "h.json")
    h = ClipboardHistory(_Settings(history_enabled=True, history_max_items=3))
    for i in range(6):
        h.add_entry(f"e{i}")
    assert h.count() == 3
