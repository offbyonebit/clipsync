"""Entry point: wires Syncthing, clipboard, and the tray icon together.

Lifecycle on start:
  1. Configure logging and settings
  2. Launch Syncthing and wait until its REST API is ready
  3. Start the auto-accepter and clipboard sync threads
  4. Run the tray icon on the main thread (required on macOS; fine
     everywhere else). UI windows are spawned as separate Python
     subprocesses by the UIController.

Lifecycle on quit:
  Tray.run() returns when `icon.stop()` is called. We then tear down
  in reverse order. Syncthing's subprocess is always the last thing we
  bring down so pending file changes flush cleanly.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import config, update
from .clipboard import ClipboardSync
from .pairing import PendingDeviceWatcher, accept_pending_device
from .single_instance import AlreadyRunning, SingleInstance
from .syncthing import SyncthingError, SyncthingService
from .ui import UIController

log = logging.getLogger(__name__)


def _load_or_create_icon(size: int = 64) -> Image.Image:
    """Return the tray icon image, generating a default if assets are missing."""
    icon_path = config.assets_dir() / "icon.png"
    if icon_path.exists():
        try:
            return Image.open(icon_path).convert("RGBA")
        except Exception:
            log.exception("Failed to load bundled icon, falling back to generated one")
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=10, fill=config.ACCENT_COLOR)
    pad = 14
    draw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=5, fill=(255, 255, 255, 230))
    for y in (pad + 6, pad + 14, pad + 22):
        draw.line((pad + 4, y, size - pad - 4, y), fill=config.ACCENT_COLOR, width=2)
    try:
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(icon_path)
    except OSError:
        pass
    return img


class ClipSyncApp:
    """Top-level orchestration object."""

    def __init__(self) -> None:
        self.settings = config.Settings()
        self.syncthing = SyncthingService(self.settings)
        self.clipboard: ClipboardSync | None = None
        self.watcher: PendingDeviceWatcher | None = None
        self.ui = UIController(on_event=self._handle_ui_event)
        self.tray: pystray.Icon | None = None
        self._quitting = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, dict[str, object]] = {}

    def start(self) -> None:
        config.configure_logging()
        config.ensure_directories()
        log.info("Starting %s v%s", config.APP_NAME, _read_version())

        self._start_syncthing_with_retry()

        assert self.syncthing.client is not None
        self.clipboard = ClipboardSync(self.settings)
        self.clipboard.start()

        self.watcher = PendingDeviceWatcher(
            self.syncthing.client,
            on_pending=self._on_pending_device,
            on_accepted=self._on_device_accepted,
            is_rejected=self._is_device_rejected,
            auto_accept=lambda: bool(self.settings.get("auto_accept_incoming")),
        )
        self.watcher.start()

        if not self.settings.get("first_run_completed"):
            self.settings.set("first_run_completed", True)
            # Tray notification needs the icon to be running, so defer.
            self._pending_first_run_notice = True
        else:
            self._pending_first_run_notice = False

        log.info("Initialization complete, starting tray")
        self._run_tray()

    def _start_syncthing_with_retry(self) -> None:
        attempt = 0
        while not self._quitting.is_set():
            try:
                self.syncthing.start()
                return
            except SyncthingError as exc:
                attempt += 1
                log.error("Syncthing failed to start (attempt %d): %s", attempt, exc)
                if self._quitting.wait(10):
                    break
            except Exception:
                log.exception("Unexpected error starting Syncthing")
                if self._quitting.wait(10):
                    break
        if not self._quitting.is_set():
            raise SystemExit("Could not start Syncthing; giving up")

    def _run_tray(self) -> None:
        image = _load_or_create_icon()
        menu = pystray.Menu(
            pystray.MenuItem(
                "Open ClipSync",
                lambda _i, _it: self.ui.open("tabbed:devices"),
                default=True,
            ),
            pystray.MenuItem(
                self._incoming_menu_title,
                lambda _i, _it: self.ui.open("incoming"),
                visible=lambda _item: self._pending_count() > 0,
            ),
            pystray.MenuItem("Add Device", lambda _i, _it: self.ui.open("tabbed:pair")),
            pystray.MenuItem("Connected Devices", lambda _i, _it: self.ui.open("tabbed:devices")),
            pystray.MenuItem(
                "Pause Sync",
                self._menu_toggle_pause,
                checked=lambda _item: bool(self.settings.get("sync_paused")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings", lambda _i, _it: self.ui.open("tabbed:settings")),
            pystray.MenuItem("Check for Updates…", self._menu_check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._menu_quit),
        )
        self.tray = pystray.Icon(
            name=config.APP_ID,
            icon=image,
            title=config.APP_NAME,
            menu=menu,
        )
        try:
            self.tray.run(setup=self._on_tray_ready)
        finally:
            self._shutdown()

    def _on_tray_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        if self._pending_first_run_notice:
            self._pending_first_run_notice = False
            self._notify(f"{config.APP_NAME} is running", "Click the tray icon to add a device.")

    def _notify(self, title: str, message: str) -> None:
        if not self.settings.get("show_notifications", True) and title != f"{config.APP_NAME} is running":
            return
        icon = self.tray
        if icon is None:
            log.info("Notification (no tray): %s — %s", title, message)
            return
        try:
            icon.notify(message, title)
        except Exception:
            log.debug("Tray notification not supported on this platform")
            log.info("%s: %s", title, message)

    def _menu_toggle_pause(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        paused = not bool(self.settings.get("sync_paused"))
        self.settings.set("sync_paused", paused)
        self._on_pause_changed(paused)
        if self.tray is not None:
            self.tray.update_menu()

    def _menu_quit(self, icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        log.info("Quit requested from tray")
        icon.stop()

    def _menu_check_updates(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self._check_updates_worker, daemon=True).start()

    def _check_updates_worker(self) -> None:
        try:
            info = update.check_for_update()
        except Exception as exc:
            log.exception("Update check failed")
            self._notify("Update check failed", f"Could not reach GitHub: {exc}")
            return
        if info.update_available:
            self._notify(
                f"Update available: v{info.latest_version}",
                "Open Settings → Check for updates to download.",
            )
        else:
            self._notify("You're up to date", f"Running the latest version (v{info.current_version}).")

    def _pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _incoming_menu_title(self, _item: pystray.MenuItem) -> str:
        count = self._pending_count()
        return f"Incoming Requests ({count})" if count else "Incoming Requests"

    def _is_device_rejected(self, device_id: str) -> bool:
        rejected = self.settings.get("rejected_device_ids") or []
        return device_id in rejected

    def _on_pending_device(self, device_id: str, info: dict[str, object]) -> None:
        """Called from the watcher thread when a new incoming request arrives."""
        with self._pending_lock:
            self._pending[device_id] = dict(info or {})
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                log.debug("Tray menu update failed", exc_info=True)
        self._notify(
            "Device wants to connect",
            f"{device_id[:7]} is requesting to sync — open the tray to accept.",
        )

    def _accept_device(self, device_id: str) -> None:
        assert self.syncthing.client is not None
        info: dict[str, object]
        with self._pending_lock:
            info = self._pending.pop(device_id, {})
        name = str(info.get("name") or "") or device_id[:7]
        try:
            accept_pending_device(self.syncthing.client, device_id, name=name)
        except Exception:
            log.exception("Failed to accept %s", device_id)
            with self._pending_lock:
                self._pending[device_id] = info
            return
        if self.watcher is not None:
            self.watcher.forget(device_id)
        self._on_device_accepted(device_id)
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def _reject_device(self, device_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(device_id, None)
        rejected = list(self.settings.get("rejected_device_ids") or [])
        if device_id not in rejected:
            rejected.append(device_id)
            self.settings.set("rejected_device_ids", rejected)
        if self.watcher is not None:
            self.watcher.forget(device_id)
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def pending_snapshot(self) -> list[dict[str, object]]:
        with self._pending_lock:
            return [{"deviceID": k, **v} for k, v in self._pending.items()]

    # UI event dispatch ------------------------------------------------------

    def _handle_ui_event(self, evt: dict) -> None:
        """Called from a background reader thread for each JSON event from a child window."""
        # Any UI event means a child subprocess may have persisted settings
        # changes to disk. Reload so in-memory values stay in sync.
        self.settings.reload()
        kind = evt.get("event")
        if kind == "pause_changed":
            self._on_pause_changed(bool(evt.get("paused")))
            if self.tray is not None:
                self.tray.update_menu()
        elif kind == "folder_changed":
            path = evt.get("path")
            if isinstance(path, str):
                self._on_folder_changed(path)
        elif kind == "reset":
            log.info("Devices reset from UI")
        elif kind == "accept_device":
            device_id = evt.get("device_id")
            if isinstance(device_id, str):
                self._accept_device(device_id)
        elif kind == "reject_device":
            device_id = evt.get("device_id")
            if isinstance(device_id, str):
                self._reject_device(device_id)
        else:
            log.debug("Unhandled UI event: %s", evt)

    def _on_pause_changed(self, paused: bool) -> None:
        if self.syncthing.client is not None:
            try:
                self.syncthing.client.set_folder_paused(paused)
            except Exception:
                log.exception("Failed to pause/resume folder on Syncthing side")
        self._notify(
            f"{config.APP_NAME} {'paused' if paused else 'resumed'}",
            "Clipboard sync is off." if paused else "Clipboard sync is on.",
        )

    def _on_device_accepted(self, device_id: str) -> None:
        self._notify("Device connected", f"Now syncing clipboard with {device_id[:7]}")

    def _on_folder_changed(self, new_path: str) -> None:
        Path(new_path).mkdir(parents=True, exist_ok=True)
        if self.clipboard is not None:
            self.clipboard.stop()
            self.clipboard = ClipboardSync(self.settings)
            self.clipboard.start()

    # Shutdown ---------------------------------------------------------------

    def _shutdown(self) -> None:
        if self._quitting.is_set():
            return
        self._quitting.set()
        log.info("Shutting down")
        try:
            self.ui.close_all()
        except Exception:
            log.exception("Error closing UI subprocesses")
        if self.watcher is not None:
            self.watcher.stop()
        if self.clipboard is not None:
            self.clipboard.stop()
        try:
            self.syncthing.stop()
        except Exception:
            log.exception("Error stopping Syncthing")
        log.info("Shutdown complete")


def _read_version() -> str:
    from . import __version__

    return __version__


def _install_signal_handlers(app: ClipSyncApp) -> None:
    def handler(_signum, _frame) -> None:
        log.info("Received termination signal, shutting down")
        if app.tray is not None:
            try:
                app.tray.stop()
                return
            except Exception:
                pass
        app._shutdown()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    config.configure_logging()
    guard = SingleInstance()
    try:
        guard.acquire()
    except AlreadyRunning:
        log.info("Another ClipSync instance is already running; exiting.")
        return 0
    try:
        app = ClipSyncApp()
        _install_signal_handlers(app)
        try:
            app.start()
        except KeyboardInterrupt:
            app._shutdown()
        except Exception:
            log.exception("Fatal error in main")
            return 1
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
