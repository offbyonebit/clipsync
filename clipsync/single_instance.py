"""Single-instance guard backed by an OS-level exclusive file lock.

A second ClipSync launch must not race the first for Syncthing's own
lock file: both Python processes end up spawning syncthing.exe in a
tight restart loop, leaving the app unusable. Acquiring an exclusive
OS lock here lets the second instance exit cleanly instead.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import BinaryIO

from . import config

log = logging.getLogger(__name__)

LOCK_FILE = config.APP_DATA_DIR / "clipsync.lock"


class AlreadyRunning(RuntimeError):
    """Raised when another ClipSync instance holds the lock."""


class SingleInstance:
    def __init__(self, path: Path = LOCK_FILE) -> None:
        self._path = path
        self._fh: BinaryIO | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("ab+")
        try:
            if platform.system() == "Windows":
                import msvcrt

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise AlreadyRunning(str(self._path)) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise AlreadyRunning(str(self._path)) from exc
        except BaseException:
            fh.close()
            raise
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if platform.system() == "Windows":
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            except Exception:
                pass
