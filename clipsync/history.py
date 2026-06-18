"""Clipboard history management.

Tracks clipboard changes and stores them in a separate JSON file so users
can access previous clipboard entries without relying on the sync engine's
last-value mechanism. Thread-safe with atomic file operations.

When an encryption passphrase is set, the history file is encrypted at rest
using the same Fernet-based scheme as the clipboard sync file.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import config
from .crypto import decrypt, encrypt, is_encrypted

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
    prevents duplicate entries from rapid polling cycles. Old entries are
    pruned automatically based on the user's auto-clear setting.
    """

    def __init__(self, settings: config.Settings | None = None) -> None:
        self._path = config.HISTORY_FILE
        self._lock = threading.RLock()
        self._entries: list[HistoryEntry] = []
        self._settings = settings
        self._max_items: int = 50 if settings is None else int(settings.get("history_max_items", 50) or 50)
        self._enabled: bool = True if settings is None else bool(settings.get("history_enabled", True))
        self._load()

    def _passphrase(self) -> str:
        if self._settings is None:
            return ""
        val = self._settings.get("encryption_passphrase") or ""
        return val if isinstance(val, str) else ""

    def _auto_clear_minutes(self) -> int:
        if self._settings is None:
            return 0
        val = self._settings.get("history_auto_clear_minutes")
        return int(val) if isinstance(val, int) and val > 0 else 0

    def _prune_old(self) -> None:
        minutes = self._auto_clear_minutes()
        if minutes <= 0:
            return
        cutoff = time.time() - (minutes * 60)
        self._entries = [e for e in self._entries if e.timestamp > cutoff]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            log.warning("Failed to read clipboard history: %s", exc)
            return

        if is_encrypted(raw):
            passphrase = self._passphrase()
            if not passphrase:
                log.warning("History file is encrypted but no passphrase is configured")
                return
            decrypted = decrypt(raw, passphrase)
            if decrypted is None:
                log.warning("Failed to decrypt clipboard history (passphrase mismatch?)")
                return
            try:
                data = json.loads(decrypted.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.warning("Failed to parse decrypted clipboard history: %s", exc)
                return
        else:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.warning("Failed to load clipboard history: %s", exc)
                return

        try:
            entries = [HistoryEntry.from_dict(e) for e in data.get("entries", [])]
            entries.sort(key=lambda e: e.timestamp)
            with self._lock:
                self._entries = entries[-self._max_items :] if len(entries) > self._max_items else entries
                self._prune_old()
        except (KeyError, ValueError) as exc:
            log.warning("Failed to load clipboard history: %s", exc)

    def _persist(self) -> None:
        if not self._enabled and len(self._entries) == 0:
            return
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"entries": [e.to_dict() for e in self._entries]}, indent=2).encode("utf-8")
            passphrase = self._passphrase()
            if passphrase:
                payload = encrypt(payload, passphrase)
            tmp.write_bytes(payload)
            tmp.replace(self._path)
            config.set_file_permissions(self._path)
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
            self._prune_old()
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
