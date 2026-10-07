"""Encryption passphrases belong in the OS keychain, never settings.json."""

from __future__ import annotations

import json

from clipsync import config, secure_settings


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


def test_upgrade_preserves_older_keychain_passphrase_and_clear_removes_it(tmp_path) -> None:
    path = tmp_path / "settings.json"
    namespace = str(path.resolve())
    secure_settings.set_passphrase("previous-release-secret", namespace)

    settings = config.Settings(path=path)
    assert settings.get_passphrase() == "previous-release-secret"
    assert "previous-release-secret" not in path.read_text()

    settings.set_passphrase("")
    assert config.Settings(path=path).get_passphrase() == ""
    assert secure_settings.get_passphrase(namespace) is None


def test_upgrade_preserves_older_encrypted_fallback_without_keychain(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    namespace = str(path.resolve())
    monkeypatch.setattr(secure_settings, "_read_machine_secret", lambda: b"test-machine")
    secure_settings._fallback_set("fallback-secret", namespace)

    def unavailable():
        raise config.SecretStorageError("not available")

    monkeypatch.setattr(config, "_keyring_backend", unavailable)
    settings = config.Settings(path=path)
    assert settings.get_passphrase() == "fallback-secret"
    assert "fallback-secret" not in path.read_text()
