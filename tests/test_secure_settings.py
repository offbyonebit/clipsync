"""Tests for secure passphrase storage.

The encryption passphrase must not be written to settings.json in plaintext.
"""

from __future__ import annotations

import json

from clipsync import config


def test_passphrase_is_not_persisted_in_settings_json(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)
    settings.set("encryption_passphrase", "my-secret-passphrase")

    persisted = json.loads(path.read_text())
    assert persisted.get("encryption_passphrase") == ""
    assert settings.get("encryption_passphrase") == "my-secret-passphrase"


def test_plaintext_passphrase_is_migrated_on_load(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"encryption_passphrase": "old-plaintext"}))

    settings = config.Settings(path=path)
    assert settings.get("encryption_passphrase") == "old-plaintext"

    persisted = json.loads(path.read_text())
    assert persisted.get("encryption_passphrase") == ""


def test_clearing_passphrase_removes_secure_storage(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)
    settings.set("encryption_passphrase", "secret")
    assert settings.get("encryption_passphrase") == "secret"

    settings.set("encryption_passphrase", "")
    assert settings.get("encryption_passphrase") == ""
