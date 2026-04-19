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
