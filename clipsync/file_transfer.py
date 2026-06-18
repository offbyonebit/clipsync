"""File transfer: send local files to connected devices and receive them.

Sending:
  copy source file into <sync_folder>/files/<sender_hostname>/<timestamp>_<name>
  Syncthing picks it up and replicates it automatically.

Receiving:
  A watchdog observer watches <sync_folder>/files/ recursively.
  Any new file under a subdirectory other than the local host's is a
  remote file.  on_received(path, sender_hostname) is called once per file.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from . import config
from .debug import _safe_hostname

log = logging.getLogger(__name__)
_HOSTNAME = _safe_hostname()


class FileTransfer:
    """Send files to the sync folder and notify on incoming files from peers."""

    def __init__(
        self,
        settings: config.Settings,
        on_received: Callable[[Path, str], None],
    ) -> None:
        self._settings = settings
        self._on_received = on_received
        self._observer: BaseObserver | None = None

    @property
    def files_dir(self) -> Path:
        folder = Path(self._settings.get("sync_folder") or config.SYNC_FOLDER)
        return folder / "files"

    def send(self, source: Path) -> Path:
        """Copy *source* into the shared folder under this host's subdirectory.

        Returns the destination path.  Raises OSError on failure.
        """
        dest_dir = self.files_dir / _HOSTNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{timestamp}_{source.name}"
        shutil.copy2(source, dest)
        log.info("FILE OUT [%s]: %s (%d bytes)", _HOSTNAME, source.name, source.stat().st_size)
        return dest

    def start(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        handler = _FileReceiveHandler(on_received=self._on_received)
        observer = Observer()
        observer.schedule(handler, str(self.files_dir), recursive=True)
        observer.start()
        self._observer = observer
        log.debug("File transfer watcher started (watching %s)", self.files_dir)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None


class _FileReceiveHandler(FileSystemEventHandler):
    """Watch the files/ tree and fire on_received for files from remote hosts."""

    def __init__(self, on_received: Callable[[Path, str], None]) -> None:
        super().__init__()
        self._on_received = on_received
        # Guard against duplicate events (watchdog can fire multiple times for
        # a single file, e.g. created + modified during Syncthing's atomic write).
        self._seen: set[str] = set()

    def _handle(self, path: Path) -> None:
        # Expected layout: files/<sender_hostname>/<filename>
        # Ignore files directly under files/ (no host subdirectory) and our own.
        sender = path.parent.name
        if not sender or sender == _HOSTNAME:
            return
        # Ignore Syncthing temp files (.syncthing.*.tmp pattern).
        if path.name.startswith(".syncthing.") and path.name.endswith(".tmp"):
            return
        key = str(path)
        if key in self._seen:
            return
        self._seen.add(key)
        log.info("FILE IN [%s]: %s from %s", _HOSTNAME, path.name, sender)
        try:
            self._on_received(path, sender)
        except Exception:
            log.exception("Error in file receive handler")

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(Path(os.fsdecode(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Syncthing uses atomic rename: .syncthing.*.tmp → final name.
        dest = getattr(event, "dest_path", "")
        if dest and not event.is_directory:
            self._handle(Path(dest))
