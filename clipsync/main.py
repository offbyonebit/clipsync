"""Entry point: wires Syncthing, clipboard, UI, and the tray icon together.

Lifecycle on start:
  1. Configure logging and settings
  2. Launch Syncthing and wait until its REST API is ready
  3. Start the auto-accepter and clipboard sync threads
  4. Show the tray icon
  5. Enter the Tk main loop (needed for CustomTkinter windows)

Lifecycle on quit:
  Stop in reverse order. Syncthing's subprocess is always the last thing
  we bring down so pending file changes flush cleanly.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import config
from .clipboard import ClipboardSync
from .pairing import PendingDeviceAccepter
from .syncthing import SyncthingError, SyncthingService
from .ui import AppContext, UIManager

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
    """Top-level orchestration object.

    Owns the Syncthing subprocess, the clipboard engine, the pending-device
    auto-accepter, the tray icon, and the UI manager. A single Event is
    used to coordinate shutdown across all threads."""

    def __init__(self) -> None:
        self.settings = config.Settings()
        self.syncthing = SyncthingService(self.settings)
        self.clipboard: ClipboardSync | None = None
        self.accepter: PendingDeviceAccepter | None = None
        self.ui = UIManager()
        self.tray: pystray.Icon | None = None
        self._quitting = threading.Event()
        self._app_context: AppContext | None = None

    def start(self) -> None:
        config.configure_logging()
        config.ensure_directories()
        log.info("Starting %s v%s", config.APP_NAME, _read_version())

        self._start_syncthing_with_retry()

        assert self.syncthing.client is not None
        self.clipboard = ClipboardSync(self.settings)
        self.clipboard.start()

        self.accepter = PendingDeviceAccepter(
            self.syncthing.client,
            on_accepted=self._on_device_accepted,
        )
        self.accepter.start()

        self._app_context = AppContext(
            settings=self.settings,
            client=self.syncthing.client,
            device_id=self.syncthing.device_id,
            on_pause_changed=self._on_pause_changed,
            on_reset=self._on_reset,
            on_folder_changed=self._on_folder_changed,
        )

        self.ui.create_root()
        self._start_tray()

        if not self.settings.get("first_run_completed"):
            self.settings.set("first_run_completed", True)
            self._notify(f"{config.APP_NAME} is running", "Click the tray icon to add a device.")

        log.info("Initialization complete, entering main loop")
        self.ui.mainloop(on_exit=self._shutdown)

    def _start_syncthing_with_retry(self) -> None:
        attempt = 0
        while not self._quitting.is_set():
            try:
                self.syncthing.start()
                return
            except SyncthingError as exc:
                attempt += 1
                log.error("Syncthing failed to start (attempt %d): %s", attempt, exc)
                self._notify("ClipSync error", f"Syncthing failed to start: {exc}. Retrying…")
                if self._quitting.wait(10):
                    break
            except Exception:
                log.exception("Unexpected error starting Syncthing")
                if self._quitting.wait(10):
                    break
        if not self._quitting.is_set():
            raise SystemExit("Could not start Syncthing; giving up")

    def _start_tray(self) -> None:
        image = _load_or_create_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Add Device", self._menu_add_device),
            pystray.MenuItem("Connected Devices", self._menu_devices),
            pystray.MenuItem(
                "Pause Sync",
                self._menu_toggle_pause,
                checked=lambda _item: bool(self.settings.get("sync_paused")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings", self._menu_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._menu_quit),
        )
        self.tray = pystray.Icon(
            name=config.APP_ID,
            icon=image,
            title=config.APP_NAME,
            menu=menu,
        )
        threading.Thread(target=self.tray.run, name="clipsync-tray", daemon=True).start()

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

    def _menu_add_device(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if self._app_context is not None:
            self.ui.open_pairing(self._app_context)

    def _menu_devices(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if self._app_context is not None:
            self.ui.open_devices(self._app_context)

    def _menu_settings(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if self._app_context is not None:
            self.ui.open_settings(self._app_context)

    def _menu_toggle_pause(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        paused = not bool(self.settings.get("sync_paused"))
        self.settings.set("sync_paused", paused)
        self._on_pause_changed(paused)
        if self.tray is not None:
            self.tray.update_menu()

    def _menu_quit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        log.info("Quit requested from tray")
        self.ui.quit()

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

    def _on_reset(self) -> None:
        if self.syncthing.client is None:
            return
        try:
            for d in self.syncthing.client.connected_devices():
                self.syncthing.client.remove_device(d["deviceID"])
            log.info("All paired devices removed")
        except Exception:
            log.exception("Failed to reset devices")

    def _on_folder_changed(self, new_path: str) -> None:
        """Update the clipboard engine to watch the new folder.

        Syncthing's own folder path change needs a restart to take effect;
        the UI surface already warns the user."""
        Path(new_path).mkdir(parents=True, exist_ok=True)
        if self.clipboard is not None:
            self.clipboard.stop()
            self.clipboard = ClipboardSync(self.settings)
            self.clipboard.start()

    def _shutdown(self) -> None:
        if self._quitting.is_set():
            return
        self._quitting.set()
        log.info("Shutting down")
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                log.exception("Error stopping tray")
        if self.accepter is not None:
            self.accepter.stop()
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
        app.ui.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    app = ClipSyncApp()
    _install_signal_handlers(app)
    try:
        app.start()
    except KeyboardInterrupt:
        app.ui.quit()
    except Exception:
        log.exception("Fatal error in main")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
