"""Clipboard sync engine.

Two loops:

  OUT: poll the local clipboard every CLIPBOARD_POLL_INTERVAL seconds.
       When it changes, write the value to the shared file.

  IN:  watch the shared file with watchdog. When it changes, read it
       and set the local clipboard.

The shared tracker (_last_synced) is the loop guard: a change is only
propagated in one direction if the text differs from the last value we
already synced, which prevents a write from causing a read from causing a
write ad infinitum.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pyperclip
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config

log = logging.getLogger(__name__)


def _normalize_newlines(s: str) -> str:
    """Collapse CRLF/CR to LF so Windows's clipboard normalization does not
    look like a real change to the OUT loop after a remote update."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


# Encrypted payloads start with this magic header so a receiver can
# detect them and either decrypt or reject without corrupting its own
# clipboard with ciphertext bytes.
_ENC_MAGIC = b"CSENC\x00"


def _derive_key(passphrase: str) -> bytes:
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"clipsync-v1-salt",
        iterations=120_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt(text: str, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    token = Fernet(_derive_key(passphrase)).encrypt(text.encode("utf-8"))
    return _ENC_MAGIC + token


def _decrypt(data: bytes, passphrase: str) -> str | None:
    from cryptography.fernet import Fernet, InvalidToken

    if not data.startswith(_ENC_MAGIC):
        return None
    try:
        plain = Fernet(_derive_key(passphrase)).decrypt(data[len(_ENC_MAGIC):])
    except (InvalidToken, ValueError):
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


class ClipboardSync:
    """Bidirectional clipboard/file sync with a shared last-value guard."""

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._observer: Observer | None = None
        self._last_synced: str | None = None
        self._lock = threading.Lock()
        self._last_read_error: str | None = None
        self._last_write_error: str | None = None
        self._last_decrypt_error: str | None = None

    @property
    def clipboard_file(self) -> Path:
        folder = Path(self._settings.get("sync_folder") or config.SYNC_FOLDER)
        return folder / config.CLIPBOARD_FILENAME

    def start(self) -> None:
        self._stop.clear()
        self._seed_from_file()
        self._poll_thread = threading.Thread(target=self._out_loop, name="clipsync-out", daemon=True)
        self._poll_thread.start()
        self._start_watcher()
        log.info("Clipboard sync started")

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                log.exception("Error stopping file observer")
            self._observer = None
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3)
        log.info("Clipboard sync stopped")

    def _passphrase(self) -> str:
        val = self._settings.get("encryption_passphrase") or ""
        return val if isinstance(val, str) else ""

    def _read_file(self) -> str | None:
        """Return the plaintext content of the shared file, transparently
        decrypting if it's a CSENC payload. None means the file is missing,
        unreadable, or encrypted with a passphrase we don't have."""
        path = self.clipboard_file
        try:
            if not path.exists():
                return None
            data = path.read_bytes()
        except OSError as exc:
            log.debug("File read failed: %s", exc)
            return None
        if data.startswith(_ENC_MAGIC):
            passphrase = self._passphrase()
            if not passphrase:
                err = "encrypted payload but no passphrase configured"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard: %s", err)
                    self._last_decrypt_error = err
                return None
            decrypted = _decrypt(data, passphrase)
            if decrypted is None:
                err = "decrypt failed (passphrase mismatch?)"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard: %s", err)
                    self._last_decrypt_error = err
                return None
            if self._last_decrypt_error is not None:
                log.info("Decrypt recovered")
                self._last_decrypt_error = None
            return _normalize_newlines(decrypted)
        try:
            return _normalize_newlines(data.decode("utf-8"))
        except UnicodeDecodeError:
            log.warning("Clipboard file is not valid UTF-8 and not encrypted; ignoring")
            return None

    def _write_file(self, text: str) -> None:
        """Atomic write of the shared file, encrypting if a passphrase is set."""
        path = self.clipboard_file
        path.parent.mkdir(parents=True, exist_ok=True)
        passphrase = self._passphrase()
        payload = _encrypt(text, passphrase) if passphrase else text.encode("utf-8")
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)

    def _seed_from_file(self) -> None:
        """Prime _last_synced from whatever is on disk so we don't re-emit it."""
        try:
            content = self._read_file()
        except Exception:
            log.warning("Could not read existing clipboard file")
            return
        if content is not None:
            with self._lock:
                self._last_synced = content

    def _is_paused(self) -> bool:
        return bool(self._settings.get("sync_paused"))

    def _read_clipboard(self) -> str | None:
        try:
            value = pyperclip.paste()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if msg != self._last_read_error:
                log.warning("Clipboard read failed: %s", msg)
                self._last_read_error = msg
            return None
        if self._last_read_error is not None:
            log.info("Clipboard read recovered")
            self._last_read_error = None
        if not isinstance(value, str):
            return None
        return _normalize_newlines(value)

    def _write_clipboard(self, value: str) -> bool:
        try:
            pyperclip.copy(value)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if msg != self._last_write_error:
                log.warning("Clipboard write failed: %s", msg)
                self._last_write_error = msg
            return False
        if self._last_write_error is not None:
            log.info("Clipboard write recovered")
            self._last_write_error = None
        return True

    def _out_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._is_paused():
                    self._out_tick()
            except Exception:
                log.exception("Error in OUT loop")
            if self._stop.wait(config.CLIPBOARD_POLL_INTERVAL):
                break

    def _out_tick(self) -> None:
        current = self._read_clipboard()
        if current is None or current == "":
            return
        with self._lock:
            if current == self._last_synced:
                return
            self._last_synced = current
        try:
            self._write_file(current)
            log.info("OUT: %d chars written", len(current))
        except OSError:
            log.exception("Failed to write clipboard file")

    def _start_watcher(self) -> None:
        handler = _ClipboardFileHandler(self)
        observer = Observer()
        folder = self.clipboard_file.parent
        folder.mkdir(parents=True, exist_ok=True)
        observer.schedule(handler, str(folder), recursive=False)
        observer.start()
        self._observer = observer

    def _on_file_changed(self) -> None:
        if self._is_paused():
            return
        content = self._read_file()
        if content is None:
            return
        with self._lock:
            if content == self._last_synced:
                return
        current = self._read_clipboard()
        if current == content:
            with self._lock:
                self._last_synced = content
            return
        if self._write_clipboard(content):
            with self._lock:
                self._last_synced = content
            log.info("IN: %d chars applied to clipboard", len(content))


class _ClipboardFileHandler(FileSystemEventHandler):
    """Dispatch watchdog events for the clipboard file to ClipboardSync."""

    def __init__(self, sync: ClipboardSync) -> None:
        super().__init__()
        self._sync = sync
        self._debounce_until = 0.0

    def _matches(self, path: str) -> bool:
        try:
            return Path(path).resolve() == self._sync.clipboard_file.resolve()
        except OSError:
            return False

    def _dispatch(self, path: str) -> None:
        if not self._matches(path):
            return
        now = time.monotonic()
        if now < self._debounce_until:
            return
        self._debounce_until = now + 0.1
        self._sync._on_file_changed()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._dispatch(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._dispatch(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "")
        if dest:
            self._dispatch(dest)
