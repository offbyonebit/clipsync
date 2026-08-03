"""An unreadable history file must never be silently overwritten.

_load() logged "Failed to decrypt clipboard history" and returned with an
empty in-memory list. Nothing marked the file as untouchable, so the very
next add_entry() persisted that empty list straight over it and every stored
entry was gone. Measured on the pre-fix code: a 5-entry encrypted history
became a 1-entry file, unrecoverable even with the correct passphrase.

The sync file has been protected from exactly this since the #21 audit, by
ClipboardSync._refuse_if_unreadable_ciphertext. The history file, which holds
the same clipboard text, was not.

These tests only ever count entries. They never assert on clipboard text.
"""

from __future__ import annotations

import json

import pytest

from clipsync import config
from clipsync.crypto import encrypt, is_encrypted
from clipsync.history import ClipboardHistory


class _Settings:
    def __init__(self, **kw):
        self._d = {"history_enabled": True, "history_max_items": 50}
        self._d.update(kw)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "hist.json")
    return tmp_path


def _seed(passphrase: str, count: int) -> ClipboardHistory:
    h = ClipboardHistory(_Settings(encryption_passphrase=passphrase))
    for i in range(count):
        h.add_entry(f"entry-{i}")
    return h


def test_passphrase_mismatch_does_not_destroy_history(_isolate):
    original = None
    _seed("right", 5)
    original = config.HISTORY_FILE.read_bytes()
    assert is_encrypted(original)

    # Reopen with the wrong passphrase, then take a new clipboard entry —
    # which is what clipboard.py does on every OUT tick.
    h2 = ClipboardHistory(_Settings(encryption_passphrase="wrong"))
    h2.add_entry("something new")

    preserved = [p for p in _isolate.iterdir() if "unreadable" in p.name]
    assert preserved, "the unreadable history was not preserved anywhere"
    assert preserved[0].read_bytes() == original, "preserved copy is not byte-identical"


def test_preserved_history_is_recoverable_with_the_right_passphrase(_isolate, monkeypatch):
    _seed("right", 5)
    ClipboardHistory(_Settings(encryption_passphrase="wrong")).add_entry("new")

    preserved = [p for p in _isolate.iterdir() if "unreadable" in p.name][0]
    monkeypatch.setattr(config, "HISTORY_FILE", preserved)
    recovered = ClipboardHistory(_Settings(encryption_passphrase="right"))
    assert recovered.count() == 5, "the preserved file did not decrypt back to the original entries"


def test_encrypted_history_with_no_passphrase_is_not_overwritten(_isolate):
    """Clearing the passphrase in settings must not wipe an encrypted history."""
    _seed("right", 3)
    original = config.HISTORY_FILE.read_bytes()

    h = ClipboardHistory(_Settings(encryption_passphrase=""))
    h.add_entry("plaintext era")

    preserved = [p for p in _isolate.iterdir() if "unreadable" in p.name]
    assert preserved, "encrypted history was overwritten once the passphrase was cleared"
    assert preserved[0].read_bytes() == original


def test_unreadable_and_unmovable_file_is_left_alone(_isolate, monkeypatch):
    """If it cannot even be moved aside, refuse to write rather than clobber."""
    _seed("right", 4)
    original = config.HISTORY_FILE.read_bytes()

    def refuse_move(self, target):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(type(config.HISTORY_FILE), "replace", refuse_move)
    h = ClipboardHistory(_Settings(encryption_passphrase="wrong"))
    h.add_entry("new entry")

    assert config.HISTORY_FILE.read_bytes() == original, "clobbered a file it could not move aside"
    assert h.count() == 1, "in-memory history should still work for this session"


def test_normal_operation_still_persists(_isolate):
    """The guard must not break the ordinary path."""
    h = _seed("right", 3)
    assert h.count() == 3
    reopened = ClipboardHistory(_Settings(encryption_passphrase="right"))
    assert reopened.count() == 3


def test_plaintext_history_still_loads_and_persists(_isolate):
    h = ClipboardHistory(_Settings(encryption_passphrase=""))
    h.add_entry("a")
    h.add_entry("b")
    assert not is_encrypted(config.HISTORY_FILE.read_bytes())
    assert ClipboardHistory(_Settings(encryption_passphrase="")).count() == 2


def test_corrupt_plaintext_json_is_not_treated_as_encrypted(_isolate):
    """A truncated plaintext file is a different failure: it carries no CSENC
    marker, so it is not quarantined, but it must not crash the app either."""
    config.HISTORY_FILE.write_bytes(b'{"entries": [')
    h = ClipboardHistory(_Settings(encryption_passphrase=""))
    assert h.count() == 0
    h.add_entry("recovered")
    assert h.count() == 1


def test_history_file_is_not_world_readable(_isolate):
    """It holds clipboard text, so it must not be readable by other local users
    at any point — including between the write and the chmod."""
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permission bits do not apply on Windows")
    _seed("right", 2)
    mode = config.HISTORY_FILE.stat().st_mode & 0o777
    assert mode == 0o600, f"history file is {oct(mode)}"


def test_future_version_ciphertext_is_preserved(_isolate):
    """A payload written by a NEWER build carries CSENC but an unknown version
    byte. is_encrypted() matches it deliberately; it must be preserved, not
    overwritten, so downgrading does not destroy the newer machine's data."""
    future = b"CSENC\x09" + b"\x00" * 32
    config.HISTORY_FILE.write_bytes(future)

    h = ClipboardHistory(_Settings(encryption_passphrase="right"))
    h.add_entry("new")

    preserved = [p for p in _isolate.iterdir() if "unreadable" in p.name]
    assert preserved, "future-version ciphertext was destroyed"
    assert preserved[0].read_bytes() == future


def test_disabled_history_does_not_clobber_existing_file(_isolate):
    _seed("right", 3)
    original = config.HISTORY_FILE.read_bytes()
    h = ClipboardHistory(_Settings(encryption_passphrase="wrong", history_enabled=False))
    h.add_entry("ignored")
    surviving = config.HISTORY_FILE.exists() and config.HISTORY_FILE.read_bytes() == original
    preserved = [p for p in _isolate.iterdir() if "unreadable" in p.name]
    assert surviving or preserved, "history was destroyed while disabled"


def test_plaintext_history_is_encrypted_once_a_passphrase_is_set(_isolate):
    """Turning encryption on must actually protect what is already stored."""
    h = ClipboardHistory(_Settings(encryption_passphrase=""))
    h.add_entry("before")
    assert not is_encrypted(config.HISTORY_FILE.read_bytes())

    h2 = ClipboardHistory(_Settings(encryption_passphrase="now-secret"))
    assert h2.count() == 1, "existing plaintext entries were lost when encryption was enabled"
    h2.add_entry("after")
    assert is_encrypted(config.HISTORY_FILE.read_bytes()), "history stayed plaintext after enabling encryption"


def test_entry_cap_is_honoured(_isolate):
    h = ClipboardHistory(_Settings(encryption_passphrase="right", history_max_items=5))
    for i in range(12):
        h.add_entry(f"e{i}")
    assert h.count() == 5
    raw = config.HISTORY_FILE.read_bytes()
    assert is_encrypted(raw)


def test_load_of_valid_but_empty_file(_isolate):
    config.HISTORY_FILE.write_bytes(json.dumps({"entries": []}).encode())
    assert ClipboardHistory(_Settings(encryption_passphrase="")).count() == 0


def test_encrypt_roundtrip_used_by_history(_isolate):
    """Sanity check on the primitive the guard depends on."""
    from clipsync.crypto import decrypt

    blob = encrypt(b'{"entries": []}', "pw")
    assert is_encrypted(blob)
    assert decrypt(blob, "pw") == b'{"entries": []}'
    assert decrypt(blob, "other") is None
