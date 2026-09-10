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

import json
import logging
import os
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from . import config
from .crypto import encrypt_file
from .debug import _safe_hostname

log = logging.getLogger(__name__)
_HOSTNAME = _safe_hostname()

# Marks a file in the shared folder as encrypted. Stripped when writing the
# plaintext out to the receiver's Downloads folder.
ENCRYPTED_SUFFIX = ".csenc"
MAX_FILE_TRANSFER_BYTES = 1 * 1024 * 1024 * 1024
MAX_INCOMING_FILES_PER_MINUTE = 30


class FileTransfer:
    """Send files to the sync folder and notify on incoming files from peers."""

    def __init__(
        self,
        settings: config.Settings,
        on_received: Callable[[Path, str], bool | None],
        expected_receivers: Callable[[], int] | None = None,
        acknowledger_id: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._on_received = on_received
        self._expected_receivers = expected_receivers or (lambda: 1)
        self._acknowledger_id = acknowledger_id or (lambda: _HOSTNAME)
        self._observer: BaseObserver | None = None
        self._stop = threading.Event()
        self._receive_queue: queue.Queue[tuple[Path, str, int]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._state: dict[str, dict[str, object]] = self._load_state()
        self._received_at: deque[float] = deque()

    @property
    def files_dir(self) -> Path:
        folder = Path(self._settings.get("sync_folder") or config.SYNC_FOLDER)
        return folder / "files"

    def _passphrase(self) -> str:
        val = self._settings.get("encryption_passphrase") or ""
        return val if isinstance(val, str) else ""

    @staticmethod
    def _validate_outgoing_source(source: Path) -> int:
        if not source.is_file() or source.is_symlink():
            raise ValueError("Only regular files can be sent")
        size = source.stat().st_size
        if size > MAX_FILE_TRANSFER_BYTES:
            raise ValueError(f"File exceeds the {MAX_FILE_TRANSFER_BYTES // (1024 * 1024)} MiB transfer limit")
        return size

    def send(self, source: Path) -> Path:
        """Copy *source* into the shared folder under this host's subdirectory.

        When a passphrase is configured the file is encrypted on the way in and
        gains a ``.csenc`` suffix. Previously it was copied verbatim, so
        enabling at-rest encryption protected the clipboard but left every sent
        file sitting in the synced folder as plaintext -- readable by anything
        with access to that directory, and replicated that way to each peer.

        Returns the destination path. Raises OSError on failure.
        """
        transfer_id = uuid.uuid4().hex
        passphrase = self._passphrase()
        size = self._validate_outgoing_source(source)
        self._set_state(
            transfer_id,
            name=source.name,
            size=size,
            status="queued",
            created=time.time(),
            acknowledgements=0,
            expected_acknowledgements=self._receiver_count(),
            source=str(source),
        )
        return self._publish_transfer(transfer_id, source, size, passphrase)

    def _receiver_count(self) -> int:
        try:
            return max(0, int(self._expected_receivers()))
        except (OSError, TypeError, ValueError):
            log.warning("Could not determine how many devices should acknowledge the transfer")
            return 0

    def _publish_transfer(self, transfer_id: str, source: Path, size: int, passphrase: str) -> Path:
        dest_dir = self.files_dir / _HOSTNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")

        if passphrase:
            dest = dest_dir / f"{transfer_id}_{timestamp}_{source.name}{ENCRYPTED_SUFFIX}"
            tmp = dest_dir / f".syncthing.{dest.name}.tmp"
            try:
                encrypt_file(source, tmp, passphrase)
                config.set_file_permissions(tmp)
                tmp.replace(dest)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                self._set_state(transfer_id, status="failed", error=str(exc))
                raise
            log.info("FILE OUT [%s]: %s (%d bytes, encrypted)", _HOSTNAME, source.name, size)
            self._set_state(transfer_id, status="transferring", path=str(dest), source="", error="")
            return dest

        dest = dest_dir / f"{transfer_id}_{timestamp}_{source.name}"
        tmp = dest_dir / f".syncthing.{dest.name}.tmp"
        try:
            shutil.copy2(source, tmp)
            config.set_file_permissions(tmp)
            tmp.replace(dest)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            self._set_state(transfer_id, status="failed", error=str(exc))
            raise
        log.info("FILE OUT [%s]: %s (%d bytes)", _HOSTNAME, source.name, size)
        self._set_state(transfer_id, status="transferring", path=str(dest), source="", error="")
        return dest

    def start(self) -> None:
        self._stop.clear()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        handler = _FileReceiveHandler(on_received=self._enqueue_receive, files_dir=self.files_dir)
        observer = Observer()
        observer.schedule(handler, str(self.files_dir), recursive=True)
        observer.start()
        self._observer = observer
        self._worker = threading.Thread(target=self._receive_loop, name="clipsync-files", daemon=True)
        self._worker.start()
        self._recover_outgoing()
        self._recover_pending(handler)
        self.cleanup_delivered()
        log.debug("File transfer watcher started (watching %s)", self.files_dir)

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        self._receive_queue.put((Path(), "", -1))
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=3)
        self._worker = None

    def _load_state(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(config.TRANSFER_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _persist_state_locked(self) -> None:
        path = config.TRANSFER_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        config.set_file_permissions(tmp)
        tmp.replace(path)

    def _set_state(self, transfer_id: str, **updates: object) -> None:
        with self._state_lock:
            self._state.setdefault(transfer_id, {}).update(updates)
            self._persist_state_locked()

    @staticmethod
    def _transfer_id(path: Path) -> str:
        candidate = path.name.split("_", 1)[0]
        if len(candidate) == 32 and all(char in "0123456789abcdef" for char in candidate.lower()):
            return candidate
        import hashlib

        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]

    def _ack_path(self, transfer_id: str) -> Path:
        try:
            raw_id = self._acknowledger_id()
        except (OSError, TypeError, ValueError):
            raw_id = _HOSTNAME
        safe_id = "".join(char for char in str(raw_id) if char.isalnum() or char in "-_") or _HOSTNAME
        return self.files_dir / ".acks" / transfer_id / f"{safe_id}.ack"

    def _enqueue_receive(self, path: Path, sender: str) -> None:
        if not self._ack_path(self._transfer_id(path)).exists():
            self._receive_queue.put((path, sender, 0))

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            path, sender, attempt = self._receive_queue.get()
            if attempt < 0 or self._stop.is_set():
                return
            now = time.monotonic()
            while self._received_at and self._received_at[0] <= now - 60:
                self._received_at.popleft()
            if len(self._received_at) >= MAX_INCOMING_FILES_PER_MINUTE and self._stop.wait(
                max(0.1, 60 - (now - self._received_at[0]))
            ):
                return
            self._received_at.append(time.monotonic())
            self._receive_once(path, sender, attempt)

    def _receive_once(self, path: Path, sender: str, attempt: int) -> None:
        try:
            result = self._on_received(path, sender)
            if result is False:
                raise OSError("receiver did not save the file")
        except Exception:
            if attempt < 4 and not self._stop.is_set():
                timer = threading.Timer(
                    2**attempt,
                    self._receive_queue.put,
                    args=((path, sender, attempt + 1),),
                )
                timer.daemon = True
                timer.start()
            else:
                log.exception("File receive failed after %d attempts: %s", attempt + 1, path)
            return
        self._write_ack(path)

    def _write_ack(self, path: Path, attempt: int = 0) -> None:
        if self._stop.is_set():
            return
        ack = self._ack_path(self._transfer_id(path))
        tmp = ack.with_name(f".syncthing.{ack.name}.{os.getpid()}.tmp")
        try:
            ack.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(str(time.time()), encoding="ascii")
            config.set_file_permissions(tmp)
            tmp.replace(ack)
        except OSError:
            tmp.unlink(missing_ok=True)
            if attempt < 4 and not self._stop.is_set():
                timer = threading.Timer(2**attempt, self._write_ack, args=(path, attempt + 1))
                timer.daemon = True
                timer.start()
            else:
                log.exception("Could not acknowledge received file after %d attempts: %s", attempt + 1, path)

    def _recover_pending(self, handler: _FileReceiveHandler) -> None:
        for sender_dir in self.files_dir.iterdir():
            if not sender_dir.is_dir() or sender_dir.name in {_HOSTNAME, ".acks"}:
                continue
            for path in sender_dir.iterdir():
                handler._handle(path)

    def _recover_outgoing(self) -> None:
        for transfer_id, item in list(self._state.items()):
            if item.get("status") != "queued":
                continue
            source = Path(str(item.get("source", "")))
            try:
                size = self._validate_outgoing_source(source)
            except (OSError, ValueError):
                self._set_state(transfer_id, status="failed", error="source file is no longer available")
                continue
            try:
                self._publish_transfer(transfer_id, source, size, self._passphrase())
            except OSError:
                log.exception("Could not recover queued transfer %s", transfer_id)

    def status_snapshot(self) -> list[dict[str, object]]:
        with self._state_lock:
            changed = False
            for transfer_id, item in self._state.items():
                ack_dir = self.files_dir / ".acks" / transfer_id
                acknowledgements = len(list(ack_dir.glob("*.ack"))) if ack_dir.exists() else 0
                expected = item.get("expected_acknowledgements", 1)
                expected_count = expected if isinstance(expected, int) else 1
                if expected_count <= 0:
                    refreshed_count = self._receiver_count()
                    if refreshed_count > 0:
                        expected_count = refreshed_count
                        item["expected_acknowledgements"] = refreshed_count
                        changed = True
                if expected_count > 0 and acknowledgements >= expected_count and item.get("status") != "delivered":
                    item["status"] = "delivered"
                    item["delivered_at"] = time.time()
                    changed = True
                if item.get("acknowledgements") != acknowledgements:
                    item["acknowledgements"] = acknowledgements
                    changed = True
            if changed:
                self._persist_state_locked()
            return [dict(item, id=transfer_id) for transfer_id, item in self._state.items()]

    def status_text(self) -> str:
        items = self.status_snapshot()
        pending = sum(item.get("status") in {"queued", "transferring"} for item in items)
        delivered = sum(item.get("status") == "delivered" for item in items)
        failed = sum(item.get("status") == "failed" for item in items)
        suffix = f" · {failed} failed" if failed else ""
        return f"Transfers: {pending} awaiting acknowledgement · {delivered} delivered{suffix}"

    def cleanup_delivered(self) -> None:
        if not self._settings.get("cleanup_delivered_transfers", True):
            return
        try:
            days = max(1, int(self._settings.get("transfer_retention_days", 7)))
        except (TypeError, ValueError):
            days = 7
        cutoff = time.time() - days * 86400
        expired_ids: list[str] = []
        for item in self.status_snapshot():
            delivered_at = item.get("delivered_at")
            if item.get("status") != "delivered" or not isinstance(delivered_at, (int, float)) or delivered_at > cutoff:
                continue
            path = Path(str(item.get("path", "")))
            try:
                if path.is_file() and path.resolve().is_relative_to((self.files_dir / _HOSTNAME).resolve()):
                    path.unlink()
            except OSError:
                log.warning("Could not clean up delivered transfer %s", item.get("id"))
                continue
            transfer_id = str(item.get("id", ""))
            ack_dir = self.files_dir / ".acks" / transfer_id
            try:
                if ack_dir.is_dir():
                    shutil.rmtree(ack_dir)
            except OSError:
                log.warning("Could not clean up acknowledgements for transfer %s", transfer_id)
                continue
            expired_ids.append(transfer_id)
        if expired_ids:
            with self._state_lock:
                for transfer_id in expired_ids:
                    self._state.pop(transfer_id, None)
                self._persist_state_locked()


class _FileReceiveHandler(FileSystemEventHandler):
    """Watch the files/ tree and fire on_received for files from remote hosts."""

    def __init__(self, on_received: Callable[[Path, str], bool | None], files_dir: Path | None = None) -> None:
        super().__init__()
        self._on_received = on_received
        # Guard against duplicate events (watchdog can fire multiple times for
        # a single file, e.g. created + modified during Syncthing's atomic write).
        # watchdog dispatches from a thread pool on Windows, so the
        # check-then-add below has to be atomic or two events for the same
        # file can both pass it and deliver the file twice.
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._files_dir = files_dir.resolve() if files_dir is not None else None
        self._received_at: deque[float] = deque()
        self._deferred: deque[tuple[Path, str]] = deque()
        self._defer_timer: threading.Timer | None = None

    def _within_rate_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        while self._received_at and self._received_at[0] <= cutoff:
            self._received_at.popleft()
        if len(self._received_at) >= MAX_INCOMING_FILES_PER_MINUTE:
            return False
        self._received_at.append(now)
        return True

    def _handle(self, path: Path) -> None:
        # Expected layout: files/<sender_hostname>/<filename>
        # Ignore files directly under files/ (no host subdirectory) and our own.
        if self._files_dir is not None:
            try:
                relative = path.resolve().relative_to(self._files_dir)
            except OSError:
                log.warning("Ignoring unreadable received file: %s", path)
                return
            except ValueError:
                log.warning("Ignoring received file outside the transfer directory: %s", path)
                return
            if relative.parts and relative.parts[0] == ".acks":
                return
            if len(relative.parts) != 2:
                log.warning("Ignoring unexpected received-file path: %s", path)
                return
        sender = path.parent.name
        if not sender or sender == _HOSTNAME:
            return
        # Ignore Syncthing temp files (.syncthing.*.tmp pattern).
        if path.name.startswith(".syncthing.") and path.name.endswith(".tmp"):
            return
        try:
            if path.is_symlink() or not path.is_file():
                log.warning("Ignoring non-regular received file: %s", path)
                return
            if path.stat().st_size > MAX_FILE_TRANSFER_BYTES:
                log.warning("Ignoring received file above transfer limit: %s", path)
                return
        except OSError:
            log.warning("Ignoring unreadable received file: %s", path)
            return
        key = str(path)
        with self._seen_lock:
            if key in self._seen:
                return
            if not self._within_rate_limit():
                self._seen.add(key)
                self._deferred.append((path, sender))
                if self._defer_timer is None:
                    delay = max(0.1, 60 - (time.monotonic() - self._received_at[0]))
                    self._defer_timer = threading.Timer(delay, self._retry_deferred)
                    self._defer_timer.daemon = True
                    self._defer_timer.start()
                log.warning("Incoming file queued until the receive rate limit resets: %s", path.name)
                return
            self._seen.add(key)
        log.info("FILE IN [%s]: %s from %s", _HOSTNAME, path.name, sender)
        try:
            self._on_received(path, sender)
        except Exception:
            log.exception("Error in file receive handler")

    def _retry_deferred(self) -> None:
        with self._seen_lock:
            self._defer_timer = None
            if not self._deferred:
                return
            path, _sender = self._deferred.popleft()
            self._seen.discard(str(path))
        self._handle(path)
        with self._seen_lock:
            if self._deferred and self._defer_timer is None:
                self._defer_timer = threading.Timer(0.1, self._retry_deferred)
                self._defer_timer.daemon = True
                self._defer_timer.start()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(Path(os.fsdecode(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Syncthing uses atomic rename: .syncthing.*.tmp → final name.
        dest = getattr(event, "dest_path", "")
        if dest and not event.is_directory:
            self._handle(Path(dest))
