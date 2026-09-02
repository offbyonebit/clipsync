"""Unit tests for the tray's live sync-health summary."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from clipsync.main import ClipSyncApp


def _app(*, paused: bool = False, client: object | None = None) -> ClipSyncApp:
    """Build the small portion of ClipSyncApp needed by status methods."""
    app = ClipSyncApp.__new__(ClipSyncApp)
    app.settings = SimpleNamespace(get=lambda key: paused if key == "sync_paused" else None)
    app.syncthing = SimpleNamespace(client=client)
    app.tray = None
    app._status_lock = threading.Lock()
    app._sync_status = "Starting…"
    return app


def test_status_reports_paused_without_calling_syncthing() -> None:
    app = _app(paused=True)
    app._refresh_sync_status()
    assert app._sync_status == "Sync paused"


def test_status_reports_waiting_when_no_peer_is_connected() -> None:
    app = _app(client=SimpleNamespace(connected_devices=lambda: [{"connected": False}]))
    app._refresh_sync_status()
    assert app._sync_status == "Waiting for a connected device"


def test_status_reports_connected_device_count() -> None:
    app = _app(client=SimpleNamespace(connected_devices=lambda: [{"connected": True}, {"connected": True}]))
    app._refresh_sync_status()
    assert app._sync_status == "Synced · 2 devices connected"


def test_status_reports_unavailable_when_syncthing_probe_fails() -> None:
    def fail() -> list[dict[str, bool]]:
        raise RuntimeError("connection refused")

    app = _app(client=SimpleNamespace(connected_devices=fail))
    app._refresh_sync_status()
    assert app._sync_status == "Sync status unavailable"
