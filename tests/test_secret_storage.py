"""Encryption passphrases belong in the OS keychain, never settings.json."""

from __future__ import annotations

import json

from clipsync import config


def test_new_passphrase_is_stored_outside_settings_json(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = config.Settings(path=path)

    settings.set_passphrase("correct horse battery staple")

    assert settings.get_passphrase() == "correct horse battery staple"
    assert "correct horse battery staple" not in path.read_text()
    assert "encryption_passphrase" not in json.loads(path.read_text())


def test_legacy_plaintext_passphrase_is_migrated_and_removed(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**config.DEFAULT_SETTINGS, "encryption_passphrase": "legacy-secret"}))

    settings = config.Settings(path=path)

    assert settings.get_passphrase() == "legacy-secret"
    assert "encryption_passphrase" not in json.loads(path.read_text())


def test_legacy_value_remains_available_if_keychain_is_unavailable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**config.DEFAULT_SETTINGS, "encryption_passphrase": "legacy-secret"}))

    def unavailable():
        raise config.SecretStorageError("not available")

    monkeypatch.setattr(config, "_keyring_backend", unavailable)
    settings = config.Settings(path=path)

    assert settings.get_passphrase() == "legacy-secret"
    assert json.loads(path.read_text())["encryption_passphrase"] == "legacy-secret"
