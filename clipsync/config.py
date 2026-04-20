"""Application configuration, paths, and constants.

Central module for all filesystem paths, runtime constants, and the
persistent JSON settings file. Kept import-light so every other module can
depend on it without cycles.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

APP_NAME = "ClipSync"
APP_ID = "clipsync"

ACCENT_COLOR = "#1A6B8A"
ACCENT_HOVER = "#145670"

SYNCTHING_VERSION = "v1.27.10"
SYNCTHING_API_HOST = "127.0.0.1"
SYNCTHING_API_PORT = 8385
SYNCTHING_API_URL = f"http://{SYNCTHING_API_HOST}:{SYNCTHING_API_PORT}"

CLIPBOARD_FOLDER_ID = "clipsync"
CLIPBOARD_FILENAME = "clipboard.txt"
CLIPBOARD_IMAGE_FILENAME = "clipboard.png"
CLIPBOARD_POLL_INTERVAL = 0.5

PAIRING_POLL_INTERVAL = 5.0

PAIRING_WINDOW_SIZE = (420, 620)
SETTINGS_WINDOW_SIZE = (420, 560)


def _app_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Roaming" / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_ID
    return Path.home() / ".config" / APP_ID


APP_DATA_DIR = _app_data_dir()
SYNCTHING_HOME = APP_DATA_DIR / "syncthing_home"
SYNCTHING_BIN_DIR = APP_DATA_DIR / "syncthing"
SYNC_FOLDER = APP_DATA_DIR / "sync"
LOG_FILE = APP_DATA_DIR / "clipsync.log"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"


def assets_dir() -> Path:
    """Return the bundled assets directory, handling PyInstaller one-file mode."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    return Path(__file__).resolve().parent / "assets"


DEFAULT_SETTINGS: dict[str, Any] = {
    "api_key": "",
    "sync_paused": False,
    "show_notifications": True,
    "start_on_login": False,
    "sync_folder": str(SYNC_FOLDER),
    "first_run_completed": False,
    "encryption_passphrase": "",
    "auto_accept_incoming": False,
    "rejected_device_ids": [],
}


class Settings:
    """Thread-safe JSON-backed settings store.

    Reads once at construction, persists on every mutation. All access is
    guarded by a lock so the tray thread, UI thread, and sync engine can
    safely share a single instance.
    """

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._mtime_ns: int = 0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            if not self._data["api_key"]:
                self._data["api_key"] = uuid.uuid4().hex
            self._persist_locked()
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to read settings, using defaults: %s", exc)
            loaded = {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
        if not merged.get("api_key"):
            merged["api_key"] = uuid.uuid4().hex
        self._data = merged
        self._persist_locked()

    def _persist_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self._path)
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            pass

    def _refresh_if_changed(self) -> None:
        """Reload from disk if another process (e.g. a UI subprocess) has
        written a newer settings.json. Cheap stat call; no-op if unchanged."""
        try:
            current_mtime = self._path.stat().st_mtime_ns
        except OSError:
            return
        if current_mtime == self._mtime_ns:
            return
        self.reload()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._refresh_if_changed()
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._persist_locked()

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._persist_locked()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def reload(self) -> None:
        """Re-read from disk. Normally called automatically by get(),
        but still available for explicit refresh (e.g. after a UI event)."""
        with self._lock:
            if not self._path.exists():
                return
            try:
                with self._path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self._mtime_ns = self._path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError) as exc:
                logging.warning("Failed to reload settings: %s", exc)
                return
            merged = dict(DEFAULT_SETTINGS)
            merged.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
            self._data = merged


def ensure_directories() -> None:
    """Create all app directories. Safe to call repeatedly."""
    for directory in (APP_DATA_DIR, SYNCTHING_HOME, SYNCTHING_BIN_DIR, SYNC_FOLDER):
        directory.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Wire up root logger to write to both the log file and stderr."""
    ensure_directories()
    root = logging.getLogger()
    if getattr(root, "_clipsync_configured", False):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.handlers = [file_handler, stream_handler]
    root._clipsync_configured = True  # type: ignore[attr-defined]


def platform_binary_name() -> str:
    return "syncthing.exe" if platform.system() == "Windows" else "syncthing"


def syncthing_binary_path() -> Path:
    return SYNCTHING_BIN_DIR / platform_binary_name()
