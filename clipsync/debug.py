"""Cross-machine log mirror for remote debugging.

Each clipsync instance periodically copies a tail of its own
`clipsync.log` into `{sync_folder}/debug/{hostname}.log`. Because that
folder is already replicated by Syncthing, every peer ends up with a
live view of every other peer's log without anyone remoting in.

Running `python -m clipsync.debug` tails all mirrored logs in the sync
folder and prints them merged chronologically.
"""

from __future__ import annotations

import io
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_MIRROR_INTERVAL_SEC = 10.0
_TAIL_BYTES = 20_000


def _safe_hostname() -> str:
    raw = socket.gethostname() or "unknown"
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    return cleaned[:64] or "unknown"


def _debug_dir(settings: config.Settings) -> Path:
    sync_folder = Path(settings.get("sync_folder") or config.SYNC_FOLDER)
    return sync_folder / "debug"


class LogMirror:
    """Copy a recent slice of clipsync.log to the shared sync folder.

    Kept intentionally simple: atomic write of the last N bytes every
    interval. Writing our own file (keyed by hostname) avoids
    sync-conflicts with peers.
    """

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hostname = _safe_hostname()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="clipsync-logmirror", daemon=True)
        self._thread.start()
        log.info("LogMirror started (host=%s)", self._hostname)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        # First tick immediately so remote observers see us promptly.
        while True:
            try:
                self._tick()
            except Exception:
                log.debug("LogMirror tick failed", exc_info=True)
            if self._stop.wait(_MIRROR_INTERVAL_SEC):
                return

    def _tick(self) -> None:
        src = config.LOG_FILE
        if not src.exists():
            return
        dest_dir = _debug_dir(self._settings)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{self._hostname}.log"

        with src.open("rb") as fh:
            fh.seek(0, io.SEEK_END)
            size = fh.tell()
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
        # Chop leading partial line so consumers don't see a half-line.
        if start > 0:
            nl = data.find(b"\n")
            if nl >= 0:
                data = data[nl + 1 :]

        tmp = dest.with_suffix(".log.tmp")
        tmp.write_bytes(data)
        # On Windows a concurrent reader (tail/editor) can momentarily
        # block the replace; a short retry keeps the mirror from dying.
        for attempt in range(5):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)


def _merged_tail(paths: list[Path], lines_per_file: int = 200) -> list[str]:
    """Load trailing N lines from each path and merge by leading ISO date."""
    all_lines: list[tuple[str, str, str]] = []  # (timestamp, host, line)
    for path in paths:
        host = path.stem
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        recent = text.splitlines()[-lines_per_file:]
        for line in recent:
            # Our log format starts with "YYYY-MM-DD HH:MM:SS"
            ts = line[:19] if len(line) >= 19 else ""
            all_lines.append((ts, host, line))
    all_lines.sort(key=lambda item: item[0])
    width = max((len(h) for _, h, _ in all_lines), default=8)
    return [f"{h:<{width}} | {line}" for _, h, line in all_lines]


def _cli_show() -> int:
    settings = config.Settings()
    dest_dir = _debug_dir(settings)
    if not dest_dir.is_dir():
        print(f"No debug folder yet at {dest_dir}")
        return 0
    paths = sorted(dest_dir.glob("*.log"))
    if not paths:
        print(f"No mirrored logs yet in {dest_dir}")
        return 0
    for line in _merged_tail(paths):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        print("Usage: python -m clipsync.debug [show]")
        print("  show (default): print merged tail of all mirrored logs")
        return 0
    return _cli_show()


if __name__ == "__main__":
    sys.exit(main())
