"""Clipboard sync engine.

Two loops:

  OUT: poll the local clipboard every CLIPBOARD_POLL_INTERVAL seconds.
       When it changes, write the value to the shared file.

  IN:  watch the shared file with watchdog. When it changes, read it
       and set the local clipboard.

The shared tracker (_last_synced) is the loop guard: a change is only
propagated in one direction if the content differs from the last value we
already synced, which prevents a write from causing a read from causing a
write ad infinitum.

Image support uses a separate file (clipboard.png) alongside the existing
clipboard.txt. Text and images are handled independently; whichever file
changes triggers the appropriate IN handler.
"""

from __future__ import annotations

import io
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pyperclip
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config
from .crypto import decrypt, encrypt, is_encrypted
from .debug import _safe_hostname
from .history import ClipboardHistory

log = logging.getLogger(__name__)

_HOSTNAME = _safe_hostname()


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _normalize_newlines(s: str) -> str:
    """Collapse CRLF/CR to LF so Windows's clipboard normalization does not
    look like a real change to the OUT loop after a remote update."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _read_image_from_system_clipboard() -> bytes | None:
    """Return PNG bytes from the system clipboard, or None if no image is present."""
    if sys.platform in ("win32", "darwin"):
        try:
            from PIL import Image, ImageGrab

            img = ImageGrab.grabclipboard()
            if not isinstance(img, Image.Image):
                return None
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None
    # Linux: try xclip then wl-paste.
    # Some xclip versions return text content with exit 0 even when asked for
    # image/png and no image is on the clipboard.  Guard with a PNG magic-byte
    # check so we never mistake text bytes for image data.
    for cmd in (
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        ["wl-paste", "--type", "image/png"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=3)
            if result.returncode == 0 and result.stdout and result.stdout[:8] == _PNG_HEADER:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def _write_image_to_system_clipboard(png_bytes: bytes) -> bool:
    """Write PNG bytes to the system clipboard. Returns True on success."""
    if sys.platform == "darwin":
        try:
            from AppKit import NSImage, NSPasteboard  # type: ignore[import]
            from Foundation import NSData  # type: ignore[import]

            ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
            ns_image = NSImage.alloc().initWithData_(ns_data)
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.writeObjects_([ns_image])
            return True
        except Exception:
            return False
    if sys.platform == "win32":
        try:
            import ctypes

            from PIL import Image

            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            bmp_buf = io.BytesIO()
            img.save(bmp_buf, format="BMP")
            dib = bmp_buf.getvalue()[14:]  # strip 14-byte BMP file header
            GMEM_MOVEABLE, CF_DIB = 0x0002, 8
            h = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
            if not h:
                return False
            p = ctypes.windll.kernel32.GlobalLock(h)
            ctypes.memmove(p, dib, len(dib))
            ctypes.windll.kernel32.GlobalUnlock(h)
            if not ctypes.windll.user32.OpenClipboard(None):
                return False
            ctypes.windll.user32.EmptyClipboard()
            ctypes.windll.user32.SetClipboardData(CF_DIB, h)
            ctypes.windll.user32.CloseClipboard()
            return True
        except Exception:
            return False
    # Linux: try xclip then wl-copy
    for cmd in (
        ["xclip", "-selection", "clipboard", "-t", "image/png"],
        ["wl-copy", "--type", "image/png"],
    ):
        try:
            result = subprocess.run(cmd, input=png_bytes, capture_output=True, timeout=3)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


class ClipboardSync:
    """Bidirectional clipboard/file sync with a shared last-value guard."""

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._in_thread: threading.Thread | None = None
        self._in_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._observer: Observer | None = None
        self._last_synced: str | bytes | None = None
        self._lock = threading.Lock()
        self._last_read_error: str | None = None
        self._last_write_error: str | None = None
        self._last_decrypt_error: str | None = None
        self._history = ClipboardHistory(settings)

    @property
    def clipboard_file(self) -> Path:
        folder = Path(self._settings.get("sync_folder") or config.SYNC_FOLDER)
        return folder / config.CLIPBOARD_FILENAME

    @property
    def clipboard_image_file(self) -> Path:
        folder = Path(self._settings.get("sync_folder") or config.SYNC_FOLDER)
        return folder / config.CLIPBOARD_IMAGE_FILENAME

    def start(self) -> None:
        self._stop.clear()
        self._seed_from_file()
        self._poll_thread = threading.Thread(target=self._out_loop, name="clipsync-out", daemon=True)
        self._poll_thread.start()
        self._in_thread = threading.Thread(target=self._in_loop, name="clipsync-in", daemon=True)
        self._in_thread.start()
        self._start_watcher()
        log.info("Clipboard sync started (host=%s)", _HOSTNAME)

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                log.exception("Error stopping file observer")
            self._observer = None
        # Unblock _in_loop which may be waiting on the queue
        self._in_queue.put("")
        if self._in_thread and self._in_thread.is_alive():
            self._in_thread.join(timeout=3)
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
        if is_encrypted(data):
            passphrase = self._passphrase()
            if not passphrase:
                err = "encrypted payload but no passphrase configured"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard: %s", err)
                    self._last_decrypt_error = err
                return None
            decrypted = decrypt(data, passphrase)
            if decrypted is None:
                err = "decrypt failed (passphrase mismatch?)"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard: %s", err)
                    self._last_decrypt_error = err
                return None
            if self._last_decrypt_error is not None:
                log.info("Decrypt recovered")
                self._last_decrypt_error = None
            try:
                return _normalize_newlines(decrypted.decode("utf-8"))
            except UnicodeDecodeError:
                log.warning("Decrypted clipboard data is not valid UTF-8; ignoring")
                return None
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
        encoded = text.encode("utf-8")
        payload = encrypt(encoded, passphrase) if passphrase else encoded
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        for attempt in range(10):
            try:
                tmp.replace(path)
                config.set_file_permissions(path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)

    def _read_image_file(self) -> bytes | None:
        """Return PNG bytes from the shared image file, decrypting if needed."""
        path = self.clipboard_image_file
        try:
            if not path.exists():
                return None
            data = path.read_bytes()
        except OSError as exc:
            log.debug("Image file read failed: %s", exc)
            return None
        if is_encrypted(data):
            passphrase = self._passphrase()
            if not passphrase:
                err = "encrypted payload but no passphrase configured"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard image: %s", err)
                    self._last_decrypt_error = err
                return None
            decrypted = decrypt(data, passphrase)
            if decrypted is None:
                err = "decrypt failed (passphrase mismatch?)"
                if err != self._last_decrypt_error:
                    log.warning("Cannot read clipboard image: %s", err)
                    self._last_decrypt_error = err
                return None
            if self._last_decrypt_error is not None:
                log.info("Decrypt recovered")
                self._last_decrypt_error = None
            data = decrypted
        if not data.startswith(_PNG_HEADER):
            log.debug("IN [%s]: clipboard.png contains non-PNG data (%d bytes); skipping", _HOSTNAME, len(data))
            return None
        return data

    def _write_image_file(self, png_bytes: bytes) -> None:
        """Atomic write of the shared image file, encrypting if a passphrase is set."""
        path = self.clipboard_image_file
        path.parent.mkdir(parents=True, exist_ok=True)
        passphrase = self._passphrase()
        payload = encrypt(png_bytes, passphrase) if passphrase else png_bytes
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        for attempt in range(10):
            try:
                tmp.replace(path)
                config.set_file_permissions(path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)

    def _seed_from_file(self) -> None:
        """Prime _last_synced from disk so we don't re-emit stale content on startup.
        Uses the more-recently-modified file when both exist."""
        try:
            text_path = self.clipboard_file
            img_path = self.clipboard_image_file
            try:
                text_mtime = text_path.stat().st_mtime if text_path.exists() else 0.0
                img_mtime = img_path.stat().st_mtime if img_path.exists() else 0.0
            except OSError:
                text_mtime = img_mtime = 0.0

            if img_mtime > text_mtime:
                image = self._read_image_file()
                if image is not None:
                    with self._lock:
                        self._last_synced = image
                    return
            content = self._read_file()
            if content is not None:
                with self._lock:
                    self._last_synced = content
        except Exception:
            log.warning("Could not read existing clipboard file")

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

    def _read_clipboard_image(self) -> bytes | None:
        try:
            return _read_image_from_system_clipboard()
        except Exception as exc:
            log.debug("Image clipboard read failed: %s", exc)
            return None

    def _write_clipboard_image(self, png_bytes: bytes) -> bool:
        try:
            return _write_image_to_system_clipboard(png_bytes)
        except Exception as exc:
            log.debug("Image clipboard write failed: %s", exc)
            return False

    def _out_loop(self) -> None:
        _heartbeat_counter = 0
        while not self._stop.is_set():
            try:
                if not self._is_paused():
                    self._out_tick()
            except Exception:
                log.exception("Error in OUT loop")
            if self._stop.wait(config.CLIPBOARD_POLL_INTERVAL):
                break
            _heartbeat_counter += 1
            if _heartbeat_counter >= 12:
                _heartbeat_counter = 0
                with self._lock:
                    last = self._last_synced
                log.info(
                    "HEARTBEAT (host=%s): last_synced=%s, paused=%s",
                    _HOSTNAME,
                    (repr(last[:40]) + "...") if isinstance(last, str) and len(last) > 40 else repr(last),
                    self._is_paused(),
                )

    def _out_tick(self) -> None:
        # Images take priority: if the clipboard has an image, sync it.
        image = self._read_clipboard_image()
        if image is not None:
            with self._lock:
                if image == self._last_synced:
                    return
                self._last_synced = image
            try:
                self._write_image_file(image)
                log.info("OUT [%s]: %d bytes image written", _HOSTNAME, len(image))
            except OSError:
                log.exception("OUT [%s]: Failed to write image file", _HOSTNAME)
            return

        current = self._read_clipboard()
        if current is None or current == "":
            return
        with self._lock:
            if current == self._last_synced:
                return
            self._last_synced = current
        try:
            self._write_file(current)
            log.info("OUT [%s]: %d chars written", _HOSTNAME, len(current))
            self._history.add_entry(current, "local")
        except OSError:
            log.exception("OUT [%s]: Failed to write clipboard file", _HOSTNAME)

    def _in_loop(self) -> None:
        """Drain _in_queue and apply remote file changes to the local clipboard.

        Watchdog dispatches events on its own internal thread (backed by a
        thread pool on Windows). Doing clipboard I/O there blocks the pool and
        causes pool-exhaustion errors. This loop runs on a thread we own so
        watchdog events are always handled off the pool in bounded time.
        """
        _last_processed: dict[str, float] = {}
        while True:
            try:
                path = self._in_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if not path:  # sentinel posted by stop()
                break
            if self._stop.is_set():
                break
            if self._is_paused():
                continue
            now = time.monotonic()
            if now - _last_processed.get(path, 0.0) < 0.1:
                continue
            _last_processed[path] = now
            try:
                self._on_file_changed(path)
            except Exception:
                log.exception("Error in IN loop")

    def _start_watcher(self) -> None:
        handler = _ClipboardFileHandler(self)
        observer = Observer()
        folder = self.clipboard_file.parent
        folder.mkdir(parents=True, exist_ok=True)
        observer.schedule(handler, str(folder), recursive=False)
        observer.start()
        self._observer = observer

    def _on_file_changed(self, path: str) -> None:
        if self._is_paused():
            return
        try:
            changed = Path(path).resolve()
        except OSError:
            return
        if changed == self.clipboard_image_file.resolve():
            self._on_image_file_changed()
        else:
            self._on_text_file_changed()

    def _on_text_file_changed(self) -> None:
        content = self._read_file()
        if content is None:
            return
        with self._lock:
            if content == self._last_synced:
                log.debug("IN [%s]: file changed but content already synced (%d chars)", _HOSTNAME, len(content))
                return
        current = self._read_clipboard()
        if current == content:
            with self._lock:
                self._last_synced = content
            log.debug("IN [%s]: file change was self-originated (%d chars)", _HOSTNAME, len(content))
            return
        if self._write_clipboard(content):
            with self._lock:
                self._last_synced = content
            log.info("IN [%s]: %d chars applied to clipboard", _HOSTNAME, len(content))
            self._history.add_entry(content, "remote")

    def _on_image_file_changed(self) -> None:
        image = self._read_image_file()
        if image is None:
            return
        with self._lock:
            if image == self._last_synced:
                log.debug("IN [%s]: image file changed but already synced (%d bytes)", _HOSTNAME, len(image))
                return
        current = self._read_clipboard_image()
        if current == image:
            with self._lock:
                self._last_synced = image
            log.debug("IN [%s]: image file change was self-originated (%d bytes)", _HOSTNAME, len(image))
            return
        if self._write_clipboard_image(image):
            with self._lock:
                self._last_synced = image
            log.info("IN [%s]: %d bytes image applied to clipboard", _HOSTNAME, len(image))


class _ClipboardFileHandler(FileSystemEventHandler):
    """Dispatch watchdog events for the clipboard files to ClipboardSync."""

    def __init__(self, sync: ClipboardSync) -> None:
        super().__init__()
        self._sync = sync
        self._debounce_until = 0.0
        # Fast name-based pre-filter to avoid Path.resolve() on every event.
        # Syncthing generates many temp-file events; most are irrelevant.
        self._target_names = {config.CLIPBOARD_FILENAME, config.CLIPBOARD_IMAGE_FILENAME}
        # Cache resolved targets once so _matches doesn't re-resolve per event.
        self._resolved_text = sync.clipboard_file.resolve()
        self._resolved_image = sync.clipboard_image_file.resolve()

    def _matches(self, path: str) -> bool:
        if Path(path).name not in self._target_names:
            return False
        try:
            resolved = Path(path).resolve()
            return resolved == self._resolved_text or resolved == self._resolved_image
        except OSError:
            return False

    def _dispatch(self, path: str) -> None:
        if not self._matches(path):
            return
        now = time.monotonic()
        if now < self._debounce_until:
            return
        self._debounce_until = now + 0.1
        # Non-blocking: hand off to _in_loop so the watchdog thread pool
        # is never held by clipboard I/O (avoids pool exhaustion on Windows).
        self._sync._in_queue.put(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._dispatch(event.src_path if isinstance(event.src_path, str) else event.src_path.decode())

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._dispatch(event.src_path if isinstance(event.src_path, str) else event.src_path.decode())

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "")
        if dest:
            self._dispatch(dest)
