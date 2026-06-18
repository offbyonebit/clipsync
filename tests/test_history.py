"""Tests for the clipboard history manager."""

from __future__ import annotations

import time

import pytest

from clipsync import config
from clipsync.history import ClipboardHistory, HistoryEntry


@pytest.fixture
def settings(tmp_path, monkeypatch) -> config.Settings:
    """Settings pointed at a tmp_path so history.json lands there."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")
    s = config.Settings(path=tmp_path / "settings.json")
    return s


def _make_settings_with(**overrides) -> config.Settings:
    """Build a Settings-like stub without touching disk for defaults."""

    values = {
        "encryption_passphrase": "",
        "history_enabled": True,
        "history_max_items": 50,
        "history_auto_clear_minutes": 0,
    }
    values.update(overrides)

    class _Stub:
        def get(self, key, default=None):
            return values.get(key, default)

    return _Stub()  # type: ignore[return-value]


def test_add_and_retrieve_entries(settings) -> None:
    h = ClipboardHistory(settings)
    h.add_entry("first", "local")
    h.add_entry("second", "remote")
    entries = h.get_entries()
    assert [e.text for e in entries] == ["first", "second"]
    assert entries[0].source == "local"
    assert entries[1].source == "remote"


def test_add_entry_dedups_consecutive_duplicates(settings) -> None:
    h = ClipboardHistory(settings)
    h.add_entry("same", "local")
    h.add_entry("same", "local")
    h.add_entry("same", "remote")  # different source but same normalized text
    h.add_entry("different", "local")
    entries = h.get_entries()
    assert [e.text for e in entries] == ["same", "different"]


def test_add_entry_dedups_after_newline_normalization(settings) -> None:
    """CRLF and LF should compare equal for dedup purposes."""
    h = ClipboardHistory(settings)
    h.add_entry("line1\nline2", "local")
    h.add_entry("line1\r\nline2", "local")
    entries = h.get_entries()
    assert len(entries) == 1


def test_add_entry_ignores_empty_text(settings) -> None:
    h = ClipboardHistory(settings)
    h.add_entry("", "local")
    assert h.get_entries() == []


def test_max_items_prunes_oldest() -> None:
    s = _make_settings_with(history_max_items=3)
    h = ClipboardHistory(s)
    for i in range(5):
        h.add_entry(f"item-{i}", "local")
    entries = h.get_entries()
    assert [e.text for e in entries] == ["item-2", "item-3", "item-4"]


def test_set_max_items_trims_existing(settings) -> None:
    h = ClipboardHistory(settings)
    for i in range(10):
        h.add_entry(f"item-{i}", "local")
    h.set_max_items(3)
    entries = h.get_entries()
    assert len(entries) == 3
    assert entries[-1].text == "item-9"


def test_clear_empties_history(settings) -> None:
    h = ClipboardHistory(settings)
    h.add_entry("a", "local")
    h.add_entry("b", "local")
    h.clear()
    assert h.get_entries() == []


def test_persistence_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")
    s = config.Settings(path=tmp_path / "settings.json")
    h = ClipboardHistory(s)
    h.add_entry("persisted", "local")

    # New instance reads from disk.
    h2 = ClipboardHistory(s)
    entries = h2.get_entries()
    assert len(entries) == 1
    assert entries[0].text == "persisted"


def test_encrypted_persistence_roundtrip(tmp_path, monkeypatch) -> None:
    """With a passphrase set, the history file must be encrypted at rest."""
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")
    s = config.Settings(path=tmp_path / "settings.json")
    s.set("encryption_passphrase", "secret-pw")

    h = ClipboardHistory(s)
    h.add_entry("secret clipboard", "local")

    raw = config.HISTORY_FILE.read_bytes()
    # Must start with the CSENC magic header — not plaintext JSON.
    assert raw.startswith(b"CSENC"), "history file should be encrypted when a passphrase is set"
    assert b"secret clipboard" not in raw, "plaintext must not appear in encrypted history file"

    # New instance with the right passphrase must decrypt and load.
    h2 = ClipboardHistory(s)
    entries = h2.get_entries()
    assert len(entries) == 1
    assert entries[0].text == "secret clipboard"


def test_encrypted_history_wrong_passphrase_is_logged_not_raised(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")
    s = config.Settings(path=tmp_path / "settings.json")
    s.set("encryption_passphrase", "right")

    ClipboardHistory(s).add_entry("data", "local")

    # Now reload with a different passphrase.
    s.set("encryption_passphrase", "wrong")
    with caplog.at_level("WARNING"):
        h2 = ClipboardHistory(s)
    assert h2.get_entries() == []
    assert any("decrypt" in r.message.lower() for r in caplog.records)


def test_auto_clear_prunes_old_entries() -> None:
    now = time.time()
    s = _make_settings_with(history_auto_clear_minutes=5)
    h = ClipboardHistory(s)
    # Inject an old entry that should be pruned (older than the 5-minute window).
    h._entries = [
        HistoryEntry(text="old", timestamp=now - 600, source="local"),
        HistoryEntry(text="recent", timestamp=now - 10, source="local"),
    ]
    h._prune_old()
    entries = h.get_entries()
    assert [e.text for e in entries] == ["recent"]


def test_auto_clear_zero_keeps_everything() -> None:
    s = _make_settings_with(history_auto_clear_minutes=0)
    h = ClipboardHistory(s)
    h._entries = [
        HistoryEntry(text="ancient", timestamp=0.0, source="local"),
    ]
    h._prune_old()
    assert len(h.get_entries()) == 1


def test_disabled_history_does_not_persist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "clipsync_history.json")
    s = config.Settings(path=tmp_path / "settings.json")
    s.set("history_enabled", False)

    h = ClipboardHistory(s)
    h.add_entry("ignored", "local")
    # History file should not have been created with payload.
    assert not config.HISTORY_FILE.exists() or config.HISTORY_FILE.read_bytes() == b""


def test_history_entry_roundtrip_dict() -> None:
    entry = HistoryEntry(text="x", timestamp=1.5, source="remote")
    d = entry.to_dict()
    restored = HistoryEntry.from_dict(d)
    assert restored == entry
    # Default source is "local" when missing from dict.
    partial = {"text": "y", "timestamp": 2.0}
    assert HistoryEntry.from_dict(partial).source == "local"
