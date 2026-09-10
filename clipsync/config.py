"""Application configuration, paths, and constants.

Central module for all filesystem paths, runtime constants, and the
persistent JSON settings file. Kept import-light so every other module can
depend on it without cycles.
"""

from __future__ import annotations

import contextlib
import hashlib
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

# Legacy accent aliases kept for backwards compatibility.
ACCENT_COLOR = "#5A6BFF"
ACCENT_HOVER = "#4654CC"

# ---------------------------------------------------------------------------
# Pro theme palette (slate + indigo).  These values are used directly by the
# UI module so changes stay centralized here instead of scattered through UI
# constructors.
# ---------------------------------------------------------------------------

COLOR_PRIMARY = "#5A6BFF"  # indigo action accent
COLOR_PRIMARY_HOVER = "#4654CC"
COLOR_PRIMARY_MUTED = (228, 231, 255)  # light-mode card tint, RGB tuple

COLOR_SUCCESS = "#2DD36F"
COLOR_DANGER = "#FF4D4D"
COLOR_DANGER_HOVER = "#CC3D3D"
COLOR_WARNING = "#FFB020"

# Light mode
COLOR_BG_LIGHT = "#F5F6F8"
COLOR_CARD_LIGHT = "#FFFFFF"
COLOR_TEXT_LIGHT = "#11131A"
COLOR_TEXT_MUTED_LIGHT = "#6B7280"
COLOR_BORDER_LIGHT = "#E2E4E9"
COLOR_ROW_BG_LIGHT = "#F0F1F5"

# Dark mode
COLOR_BG_DARK = "#0F1117"
COLOR_CARD_DARK = "#181A21"
COLOR_TEXT_DARK = "#F0F1F5"
COLOR_TEXT_MUTED_DARK = "#8B92A5"
COLOR_BORDER_DARK = "#2A2D38"
COLOR_ROW_BG_DARK = "#1E212B"

SYNCTHING_VERSION = "v2.0.16"
SYNCTHING_API_HOST = "127.0.0.1"
SYNCTHING_API_PORT = 8385
SYNCTHING_API_URL = f"http://{SYNCTHING_API_HOST}:{SYNCTHING_API_PORT}"

CLIPBOARD_FOLDER_ID = "clipsync"
CLIPBOARD_FILENAME = "clipboard.txt"
CLIPBOARD_IMAGE_FILENAME = "clipboard.png"
CLIPBOARD_POLL_INTERVAL = 0.5

PAIRING_POLL_INTERVAL = 5.0

PAIRING_WINDOW_SIZE = (420, 560)
SETTINGS_WINDOW_SIZE = (420, 480)


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
    "auto_accept_incoming": False,
    "rejected_device_ids": [],
    "history_enabled": True,
    "history_max_items": 50,
    "history_auto_clear_minutes": 0,
    "manual_send": False,
    "paused_until": 0.0,
    "max_text_bytes": 1 * 1024 * 1024,
    "max_image_bytes": 5 * 1024 * 1024,
    "history_shortcut": "<ctrl>+<alt>+v",
    "transfer_retention_days": 7,
    "cleanup_delivered_transfers": True,
    "theme": "System",
    # Mirror this device's log into the shared folder so peers can see it.
    # Off by default: it is a debugging aid, and the sync folder is replicated
    # to every paired device, so leaving it on ships your log (hostnames,
    # device IDs, file names, error traces) to all of them forever. No
    # clipboard text is ever logged, but none of that is obvious from the
    # tray, and it was previously always on with no way to turn it off.
    "debug_log_mirror": False,
}

HISTORY_FILE = APP_DATA_DIR / "clipsync_history.json"
TRANSFER_STATE_FILE = APP_DATA_DIR / "transfers.json"
_PASSPHRASE_KEYRING_ACCOUNT = "encryption-passphrase"


class SecretStorageError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


def _keyring_backend() -> Any:
    """Return the optional keyring module without importing it at startup."""
    try:
        import keyring
    except ImportError as exc:
        raise SecretStorageError("OS keychain support is not installed") from exc
    return keyring


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
        self._legacy_passphrase: str | None = None
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
            # Do NOT overwrite a corrupted/unreadable file with defaults.
            # The user may still be able to recover it manually; clobbering
            # it here turns a transient parse error into permanent data
            # loss. Stay on defaults in memory and let the next successful
            # set() re-persist.
            logging.warning("Failed to read settings, using defaults: %s", exc)
            return
        if not isinstance(loaded, dict):
            logging.warning("Settings file did not contain a JSON object; using defaults")
            return
        legacy = loaded.get("encryption_passphrase")
        self._legacy_passphrase = legacy if isinstance(legacy, str) and legacy else None
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
        if not merged.get("api_key"):
            merged["api_key"] = uuid.uuid4().hex
        self._data = merged
        self._migrate_legacy_passphrase_locked()
        # Only persist if the on-disk file is incomplete (missing a default
        # key) or has an empty api_key that we just generated. Otherwise
        # leave the file alone: rewriting it on every startup is needless
        # churn and could race with a concurrent writer (e.g. a UI
        # subprocess that just wrote a new value).
        loaded_keys = set(loaded.keys())
        needs_persist = not loaded.get("api_key") or any(k not in loaded_keys for k in DEFAULT_SETTINGS)
        if needs_persist:
            self._persist_locked()
        else:
            try:
                self._mtime_ns = self._path.stat().st_mtime_ns
            except OSError:
                pass

    def _persist_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            persisted = dict(self._data)
            if self._legacy_passphrase:
                # Retain this only when the OS keychain is unavailable. This
                # avoids turning an upgrade into unrecoverable encrypted data.
                persisted["encryption_passphrase"] = self._legacy_passphrase
            else:
                persisted.pop("encryption_passphrase", None)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(persisted, fh, indent=2)
            set_file_permissions(tmp)
            os.replace(tmp, self._path)
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
        set_file_permissions(self._path)
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
            if key == "encryption_passphrase":
                return self._get_passphrase_locked()
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key == "encryption_passphrase":
                self.set_passphrase(value)
                return
            self._data[key] = value
            self._persist_locked()

    def _get_passphrase_locked(self) -> str:
        try:
            value = _keyring_backend().get_password(APP_ID, self._keyring_account())
        except Exception as exc:
            if self._legacy_passphrase:
                logging.warning("OS keychain unavailable; retaining legacy encrypted-data access: %s", exc)
                return self._legacy_passphrase
            logging.warning("OS keychain unavailable: %s", exc)
            return ""
        if isinstance(value, str):
            return value
        return self._migrate_legacy_passphrase_locked()

    def _migrate_legacy_passphrase_locked(self) -> str:
        if not self._legacy_passphrase:
            return ""
        legacy = self._legacy_passphrase
        try:
            _keyring_backend().set_password(APP_ID, self._keyring_account(), legacy)
        except Exception as exc:
            logging.warning("Could not migrate passphrase into OS keychain: %s", exc)
            return legacy
        self._legacy_passphrase = None
        self._persist_locked()
        logging.info("Moved encryption passphrase from settings.json into the OS keychain")
        return legacy

    def _keyring_account(self) -> str:
        """Keep separate portable/test profiles from sharing a local secret."""
        profile = hashlib.sha256(str(self._path.resolve()).encode("utf-8")).hexdigest()[:16]
        return f"{_PASSPHRASE_KEYRING_ACCOUNT}-{profile}"

    def get_passphrase(self) -> str:
        """Return the encryption passphrase from the OS keychain."""
        with self._lock:
            self._refresh_if_changed()
            return self._get_passphrase_locked()

    def set_passphrase(self, value: Any) -> None:
        """Store an encryption passphrase outside settings.json.

        New values are never written to the JSON settings file. Existing
        plaintext values are migrated automatically on their first read.
        """
        if not isinstance(value, str):
            raise ValueError("Encryption passphrase must be text")
        with self._lock:
            try:
                backend = _keyring_backend()
                if value:
                    backend.set_password(APP_ID, self._keyring_account(), value)
                elif backend.get_password(APP_ID, self._keyring_account()) is not None:
                    backend.delete_password(APP_ID, self._keyring_account())
            except Exception as exc:
                raise SecretStorageError("Could not store passphrase in the OS keychain") from exc
            self._legacy_passphrase = None
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
            if not isinstance(loaded, dict):
                logging.warning("Settings file did not contain a JSON object; keeping in-memory state")
                return
            legacy = loaded.get("encryption_passphrase")
            self._legacy_passphrase = legacy if isinstance(legacy, str) and legacy else None
            merged = dict(DEFAULT_SETTINGS)
            merged.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
            self._data = merged
            self._migrate_legacy_passphrase_locked()


def ensure_directories() -> None:
    """Create all app directories. Safe to call repeatedly."""
    for directory in (APP_DATA_DIR, SYNCTHING_HOME, SYNCTHING_BIN_DIR, SYNC_FOLDER):
        directory.mkdir(parents=True, exist_ok=True)


def set_file_permissions(path: Path) -> None:
    """Restrict file access to owner-only where possible (Unix)."""
    try:
        os.chmod(path, 0o600)
    except (OSError, AttributeError):
        pass


def configure_logging() -> None:
    """Wire up root logger to write to both the log file and stderr."""
    ensure_directories()
    root = logging.getLogger()
    if getattr(root, "_clipsync_configured", False):
        return
    level = logging.INFO
    # Honour CLIPSYNC_LOG_LEVEL env var (DEBUG, INFO, WARNING, ERROR).
    if "CLIPSYNC_LOG_LEVEL" in os.environ:
        level = getattr(logging, os.environ["CLIPSYNC_LOG_LEVEL"].upper(), logging.INFO)
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(LOG_FILE, encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.handlers = [file_handler, stream_handler]
    root._clipsync_configured = True  # type: ignore[attr-defined]


def platform_binary_name() -> str:
    return "syncthing.exe" if platform.system() == "Windows" else "syncthing"


def syncthing_binary_path() -> Path:
    return SYNCTHING_BIN_DIR / platform_binary_name()
