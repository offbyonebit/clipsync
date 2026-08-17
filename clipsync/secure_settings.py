"""Secure storage for sensitive settings.

The encryption passphrase is too sensitive to keep in the plaintext
settings.json file. We store it in the OS credential store when available
(keyring), and fall back to a local encrypted file protected by a
machine-bound key.

The fallback is weaker than the OS keychain -- anyone with access to both the
encrypted file and the machine-bound key can recover the passphrase -- but it
is still a meaningful improvement over plaintext in settings.json, especially
on shared machines or backups.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import platform
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet

from . import config

log = logging.getLogger(__name__)

_KEYRING_SERVICE: Final = "offbyonebit-clipsync"
_KEYRING_USERNAME: Final = "encryption_passphrase"


def _username(namespace: str) -> str:
    return f"{_KEYRING_USERNAME}-{namespace}"


_FALLBACK_FILE: Final = config.APP_DATA_DIR / "passphrase.enc"
_FALLBACK_SALT_FILE: Final = config.APP_DATA_DIR / ".salt"


def _read_machine_secret() -> bytes:
    """Return a stable, machine-specific byte string.

    This is intentionally *not* cryptographically secret: an attacker with
    admin/root access to the machine can read it. It exists to bind the
    fallback encrypted file to this device, so a copied settings backup does
    not trivially reveal the passphrase.
    """
    candidates: list[bytes] = []
    system = platform.system()

    if system == "Windows":
        try:
            import winreg

            with winreg.OpenKey(  # type: ignore[attr-defined]
                winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")  # type: ignore[attr-defined]
                candidates.append(value.encode("utf-8"))
        except Exception:
            pass
    elif system == "Darwin":
        try:
            import subprocess

            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            # ioreg -a would give XML; -rd1 gives a text plist that
            # plistlib can parse since Python 3.9.
            candidates.append(result.stdout.encode("utf-8"))
        except Exception:
            pass

    # Linux / fallback: D-Bus or systemd machine-id.
    for machine_id_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            candidates.append(machine_id_path.read_bytes().strip())
        except OSError:
            pass

    # Last-resort fallback: hostname + username + home path. Not unique across
    # identical user accounts, but still better than a hardcoded key.
    candidates.append(f"{os.getlogin()}@{platform.node()}:{Path.home()}".encode())

    return b"\0".join(candidates)


def _derive_fallback_key(salt: bytes) -> bytes:
    """Derive a Fernet key from the machine secret + salt."""
    key = hashlib.pbkdf2_hmac("sha256", _read_machine_secret(), salt, iterations=100_000, dklen=32)
    return base64.urlsafe_b64encode(key)


def _namespace_hash(namespace: str) -> str:
    import hashlib

    return hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32]


def _fallback_file(namespace: str) -> Path:
    return _FALLBACK_FILE.with_name(f"passphrase-{_namespace_hash(namespace)}.enc")


def _fallback_salt_file(namespace: str) -> Path:
    return _FALLBACK_SALT_FILE.with_name(f".salt-{_namespace_hash(namespace)}")


def _load_or_create_salt(namespace: str) -> bytes:
    """Return the per-install salt, creating it if necessary."""
    salt_file = _fallback_salt_file(namespace)
    try:
        data = salt_file.read_bytes()
        if len(data) >= 16:
            return data
    except OSError:
        pass
    salt = os.urandom(16)
    salt_file.parent.mkdir(parents=True, exist_ok=True)
    salt_file.write_bytes(salt)
    config.set_file_permissions(salt_file)
    return salt


def _fallback_get(namespace: str) -> str | None:
    """Read the passphrase from the encrypted fallback file."""
    fallback_file = _fallback_file(namespace)
    if not fallback_file.exists():
        return None
    try:
        data = fallback_file.read_bytes()
        if not data:
            return None
        salt = _load_or_create_salt(namespace)
        fernet = Fernet(_derive_fallback_key(salt))
        return fernet.decrypt(data).decode("utf-8")
    except Exception:
        log.warning("Could not read encrypted passphrase fallback", exc_info=True)
        return None


def _fallback_set(passphrase: str | None, namespace: str) -> None:
    """Write or remove the encrypted fallback file."""
    fallback_file = _fallback_file(namespace)
    if passphrase is None or passphrase == "":
        fallback_file.unlink(missing_ok=True)
        return
    salt = _load_or_create_salt(namespace)
    fernet = Fernet(_derive_fallback_key(salt))
    fallback_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = fallback_file.with_suffix(".enc.tmp")
    tmp.write_bytes(fernet.encrypt(passphrase.encode("utf-8")))
    os.replace(tmp, fallback_file)
    config.set_file_permissions(fallback_file)


def get_passphrase(namespace: str = "default") -> str | None:
    """Return the stored encryption passphrase, or None if not set.

    Prefers the OS keychain via keyring; falls back to the local encrypted
    file if keyring is unavailable or has no usable backend.
    """
    try:
        import keyring

        value = keyring.get_password(_KEYRING_SERVICE, _username(namespace))
        if value is not None:
            return value
    except Exception:
        log.debug("keyring read failed", exc_info=True)

    return _fallback_get(namespace)


def set_passphrase(passphrase: str | None, namespace: str = "default") -> None:
    """Store or clear the encryption passphrase.

    Tries the OS keychain first. If that fails, encrypts to the local
    fallback file. Clearing the passphrase removes both stores.
    """
    if passphrase == "":
        passphrase = None

    keyring_ok = False
    try:
        import keyring

        if passphrase is None:
            try:
                keyring.delete_password(_KEYRING_SERVICE, _username(namespace))
            except Exception:
                pass
        else:
            keyring.set_password(_KEYRING_SERVICE, _username(namespace), passphrase)
        keyring_ok = True
    except Exception:
        log.debug("keyring write failed, using encrypted fallback", exc_info=True)

    if keyring_ok:
        # If we successfully moved to keyring, remove stale fallback.
        _fallback_set(None, namespace)
    else:
        _fallback_set(passphrase, namespace)


def migrate_plaintext_passphrase(settings: config.Settings, namespace: str = "default") -> bool:
    """Move a passphrase stored in settings.json into secure storage.

    Returns True if a migration happened. The plaintext field is cleared
    immediately after the secure store succeeds.
    """
    plaintext = settings._data.get("encryption_passphrase")
    if not plaintext or not isinstance(plaintext, str):
        return False
    try:
        set_passphrase(plaintext, namespace)
        settings._data["encryption_passphrase"] = ""
        log.info("Migrated encryption passphrase from settings.json into secure storage")
        return True
    except Exception:
        log.warning("Could not migrate plaintext passphrase to secure storage", exc_info=True)
        return False
