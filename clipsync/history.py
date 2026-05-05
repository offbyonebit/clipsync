"""Clipboard history management.

Tracks clipboard changes and stores them in a separate JSON file so users
can access previous clipboard entries without relying on the sync engine's
last-value mechanism. Thread-safe with atomic file operations.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    text: str
    timestamp: float
    source: str = "local"  # 'local' or 'remote'

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "timestamp": self.timestamp, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        return cls(
            text=data["text"],
            timestamp=float(data["timestamp"]),
            source=str(data.get("source", "local")),
        )


class ClipboardHistory:
    """Thread-safe clipboard history manager.

    Persists entries to a JSON file with atomic writes. Deduplication
    prevents duplicate entries from rapid polling cycles.
    """

    def __init__(self, settings: config.Settings | None = None) -> None:
        self._path = config.HISTORY_FILE
        self._lock = threading.RLock()
        self._entries: list[HistoryEntry] = []
        self._max_items: int = 50 if settings is None else int(settings.get("history_max_items", 50) or 50)
        self._enabled: bool = True if settings is None else bool(settings.get("history_enabled", True))
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = [HistoryEntry.from_dict(e) for e in data.get("entries", [])]
            entries.sort(key=lambda e: e.timestamp)
            with self._lock:
                self._entries = entries[-self._max_items :] if len(entries) > self._max_items else entries
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("Failed to load clipboard history: %s", exc)

    def _persist(self) -> None:
        if not self._enabled and len(self._entries) == 0:
            return
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"entries": [e.to_dict() for e in self._entries]}, fh, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("Failed to persist clipboard history: %s", exc)

    def add_entry(self, text: str, source: str = "local") -> None:
        if not self._enabled or not text:
            return
        with self._lock:
            if self._entries:
                last = self._entries[-1]
                if _normalize(last.text) == _normalize(text):
                    return
            self._entries.append(HistoryEntry(text=text, timestamp=time.time(), source=source))
            while len(self._entries) > self._max_items:
                self._entries.pop(0)
        self._persist()

    def get_entries(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
        self._persist()

    def get_max_items(self) -> int:
        return self._max_items

    def set_max_items(self, value: int) -> None:
        if value > 0 and value != self._max_items:
            with self._lock:
                self._max_items = value
                while len(self._entries) > self._max_items:
                    self._entries.pop(0)
            self._persist()

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value

    def count(self) -> int:
        with self._lock:
            return len(self._entries)


def _normalize(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n") if isinstance(s, str) else ""
