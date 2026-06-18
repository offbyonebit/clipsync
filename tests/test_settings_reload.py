"""Tests for the cross-process settings reload behavior.

The real-world bug: a UI subprocess writes settings.json, but the main
process's in-memory Settings is stale until something forces a reload.
This caused passphrase changes to not take effect for several minutes.

Settings.get() now stats the file and reloads on mtime change, so
changes written by a second process (or a second Settings instance
pointing at the same file) should be visible within a single get call.
"""

from __future__ import annotations

import json
import os
import time

from clipsync import config


def test_second_instance_sees_writes_from_first(tmp_path) -> None:
    path = tmp_path / "settings.json"
    a = config.Settings(path=path)
    b = config.Settings(path=path)

    a.set("encryption_passphrase", "secret-from-a")

    # Without the mtime auto-reload, b would still return "" here.
    assert b.get("encryption_passphrase") == "secret-from-a"


def test_external_file_rewrite_is_picked_up(tmp_path) -> None:
    """Simulates a UI subprocess writing settings.json with no shared memory."""
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)
    assert settings.get("encryption_passphrase") == ""

    # Pretend another process rewrites the file.
    data = json.loads(path.read_text())
    data["encryption_passphrase"] = "hello-from-ui"
    # Ensure mtime changes on filesystems with second-granularity stat.
    time.sleep(0.01)
    path.write_text(json.dumps(data))
    os.utime(path, None)

    assert settings.get("encryption_passphrase") == "hello-from-ui"


def test_repeated_gets_do_not_redundantly_reload(tmp_path, monkeypatch) -> None:
    """Stat happens on every get, but reload() itself should only fire
    when mtime actually advanced. Otherwise we churn JSON parsing."""
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)

    calls = {"count": 0}
    original_reload = settings.reload

    def counting_reload() -> None:
        calls["count"] += 1
        original_reload()

    monkeypatch.setattr(settings, "reload", counting_reload)

    for _ in range(50):
        settings.get("encryption_passphrase")

    assert calls["count"] == 0, "get() should not call reload() when mtime is unchanged"


def test_corrupted_settings_file_is_not_clobbered(tmp_path) -> None:
    """A corrupted settings.json must NOT be overwritten with defaults.

    Previously a JSONDecodeError fell through to _persist_locked(), which
    silently destroyed the user's file. The fix returns early so the
    user can recover the file manually.
    """
    path = tmp_path / "settings.json"
    path.write_text("{ this is not valid json")
    original_bytes = path.read_bytes()

    config.Settings(path=path)  # must not raise, must not overwrite

    assert path.read_bytes() == original_bytes, "corrupted settings file was clobbered"


def test_corrupted_settings_falls_back_to_defaults_in_memory(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ broken")

    settings = config.Settings(path=path)
    # In-memory state is defaults. api_key stays empty here; prepare_home()
    # regenerates one on demand the next time Syncthing starts.
    assert settings.get("sync_paused") is False
    assert settings.get("show_notifications") is True
    assert isinstance(settings.get("api_key"), str)


def test_load_does_not_persist_when_file_already_complete(tmp_path, monkeypatch) -> None:
    """A settings.json that already contains every default key must not be
    rewritten on every startup. We track writes via os.replace."""
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)  # writes once on creation
    initial_mtime_ns = path.stat().st_mtime_ns

    # Reset mtime cache to force the next _load to actually stat the file.
    settings._mtime_ns = 0
    settings._load()

    try:
        final_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return
    assert final_mtime_ns == initial_mtime_ns, "settings.json was needlessly rewritten on load"


def test_load_does_not_overwrite_when_external_writer_changes_a_value(tmp_path) -> None:
    """If another process rewrites settings.json with a different value but
    the same set of keys, _load() must pick up the new value in memory but
    must NOT persist (which would needlessly rewrite the file on every
    startup and could race with the external writer)."""
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)

    # Simulate a UI subprocess writing the file with a different value
    # but the same complete set of keys.
    data = json.loads(path.read_text())
    data["sync_paused"] = True
    import time as _time

    _time.sleep(0.01)
    path.write_text(json.dumps(data))
    mtime_after_external_write = path.stat().st_mtime_ns

    settings._mtime_ns = 0  # force re-read on next _load
    settings._load()

    # In-memory state reflects the external write...
    assert settings.get("sync_paused") is True
    # ...and we did not rewrite the file (mtime unchanged).
    assert path.stat().st_mtime_ns == mtime_after_external_write


def test_load_persists_when_default_key_missing_from_disk(tmp_path) -> None:
    """An older settings.json missing a key that was added to DEFAULT_SETTINGS
    in a later release must be repaired on disk (so the file stays complete
    for any external reader that doesn't merge defaults)."""
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)

    data = json.loads(path.read_text())
    del data["show_notifications"]  # simulate an upgrade from an older release
    path.write_text(json.dumps(data))

    settings._mtime_ns = 0
    settings._load()

    persisted = json.loads(path.read_text())
    assert "show_notifications" in persisted
    assert persisted["show_notifications"] is True
    assert settings.get("show_notifications") is True


def test_load_persists_when_api_key_missing_from_disk(tmp_path) -> None:
    """A settings.json missing the api_key must be repaired (one generated
    and written back) so Syncthing can talk to its own REST API."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sync_paused": True, "show_notifications": False}))

    settings = config.Settings(path=path)
    assert settings.get("api_key") != ""
    persisted = json.loads(path.read_text())
    assert persisted["api_key"] == settings.get("api_key")


def test_load_handles_non_object_json_without_clobbering(tmp_path) -> None:
    """A settings.json that is valid JSON but not an object (e.g. an array)
    must not be clobbered either; the same recovery principle applies."""
    path = tmp_path / "settings.json"
    original_bytes = b'["not", "an", "object"]'
    path.write_bytes(original_bytes)

    settings = config.Settings(path=path)
    assert path.read_bytes() == original_bytes
    assert settings.get("sync_paused") is False  # defaults in memory
