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


class ClipboardSync:
    """Bidirectional clipboard/file sync with a shared last-value guard."""

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._observer: Observer | None = None
        self._last_synced: str | None = None
        self._lock = threading.Lock()

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

    def _seed_from_file(self) -> None:
        """Prime _last_synced from whatever is on disk so we don't re-emit it."""
        path = self.clipboard_file
        try:
            if path.exists():
                with self._lock:
                    self._last_synced = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Could not read existing clipboard file: %s", exc)

    def _is_paused(self) -> bool:
        return bool(self._settings.get("sync_paused"))

    def _read_clipboard(self) -> str | None:
        try:
            value = pyperclip.paste()
        except Exception as exc:
            log.debug("Clipboard read failed: %s", exc)
            return None
        if not isinstance(value, str):
            return None
        return value

    def _write_clipboard(self, value: str) -> bool:
        try:
            pyperclip.copy(value)
            return True
        except Exception as exc:
            log.debug("Clipboard write failed: %s", exc)
            return False

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
        path = self.clipboard_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".txt.tmp")
            tmp.write_text(current, encoding="utf-8")
            tmp.replace(path)
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
        path = self.clipboard_file
        try:
            if not path.exists():
                return
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.debug("File read failed: %s", exc)
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
