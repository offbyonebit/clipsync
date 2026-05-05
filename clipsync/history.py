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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import config

log = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    """A single clipboard history entry."""

    text: str
    timestamp: float  # Unix timestamp in seconds
    source: str = "local"  # 'local' or 'remote'

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "timestamp": self.timestamp,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        return cls(
            text=data["text"],
            timestamp=float(data["timestamp"]),
            source=str(data.get("source", "local")),
        )


class ClipboardHistory:
    """Thread-safe clipboard history manager.

    Persists entries to a JSON file with atomic writes. Each entry includes
    the clipboard text, UTC ISO 8601 timestamp, and whether it came from
    local or remote changes. Deduplication prevents duplicate entries from
    rapid polling cycles.
    """

    def __init__(self, settings: config.Settings | None = None) -> None:
        self._history_file = Path("clipsync_history.json")
        self._settings = settings
        self._lock = threading.RLock()
        self._entries: list[HistoryEntry] = []
        self._max_items: int = 50 if settings is None else (settings.get("history_max_items", 50) or 50)
        self._enabled: bool = True if settings is None else (settings.get("history_enabled", True))
        self._load()

    def _load(self) -> None:
        """Load history from file, applying configured settings."""
        if not self._history_file.exists():
            return
        try:
            with self._history_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = [HistoryEntry.from_dict(entry) for entry in data.get("entries", [])]
            # Sort by timestamp to maintain chronological order
            entries.sort(key=lambda e: e.timestamp)
            self._entries = entries[-self._max_items :] if len(entries) > self._max_items else entries
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load clipboard history: %s", exc)

    def _persist(self) -> None:
        """Save current entries to file atomically."""
        if not self._enabled or len(self._entries) == 0:
            return
        path = self._history_file.parent / "clipsync_history.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"entries": [e.to_dict() for e in self._entries]}, fh, indent=2)
            tmp.replace(path)
        except OSError as exc:
            log.warning("Failed to persist clipboard history: %s", exc)

    def add_entry(self, text: str, source: str = "local") -> None:
        """Add a new entry if it's not a duplicate of the last one."""
        if not self._enabled:
            return
        with self._lock:
            # Skip duplicates (same content as most recent)
            if self._entries and len(self._entries) > 0:
                last = self._entries[-1]
                if _normalize_newlines(last.text) == _normalize_newlines(text):
                    return
            entry = HistoryEntry(
                text=text,
                timestamp=time.time(),
                source=source,
            )
            self._entries.append(entry)
            # Trim oldest entries if over max
            while len(self._entries) > self._max_items:
                self._entries.pop(0)
        self._persist()

    def get_entries(self) -> list[HistoryEntry]:
        """Return all history entries in chronological order."""
        with self._lock:
            return [e for e in self._entries]

    def clear_history(self) -> None:
        """Clear all history entries and persist the change."""
        with self._lock:
            self._entries.clear()
        self._persist()

    def get_configured_max_items(self) -> int:
        return self._max_items

    def set_configured_max_items(self, value: int) -> None:
        if value > 0 and value != self._max_items:
            with self._lock:
                self._max_items = min(value, len(self._entries))
            self._persist()

    def toggle_enabled(self) -> bool:
        """Toggle history tracking on/off."""
        self._enabled = not self._enabled
        return self._enabled

    def get_entry_count(self) -> int:
        return len(self._entries)


def _normalize_newlines(s: str) -> str:
    """Normalize whitespace for deduplication comparisons."""
    return s.replace("\r\n", "\n").replace("\r", "\n") if isinstance(s, str) else ""
