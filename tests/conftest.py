"""Global test isolation.

ClipboardSync builds a ClipboardHistory bound to ``config.HISTORY_FILE`` at
construction, so any test that constructs one writes to whatever that module
global points at. Several suites also set an encryption passphrase
("shared-secret", "mac-pass", ...) before syncing, so an unisolated run does
not merely touch the developer's real clipboard history: it overwrites it,
encrypted with a throwaway key, and the running app can then never read its
own history again.

That is not hypothetical. It happened: running the suite on a machine with
ClipSync installed replaced a 76KB real history with a 1698-byte file
encrypted under "shared-secret", repeatedly.

Individual modules used to patch HISTORY_FILE one by one, and four of them
(test_cross_os_sync, test_image_sync, test_linux_paste_freeze,
test_mac_windows_sync) did not. Doing it here instead makes isolation the
default for every test, present and future, rather than something each new
file has to remember.
"""

from __future__ import annotations

import pytest

from clipsync import config


class _MemoryKeyring:
    """In-memory keychain so tests never read or alter user credentials."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """Point every user-data path at a per-test temp directory.

    Autouse and unconditional: a test that wants the real paths would have to
    opt in explicitly, which nothing should ever do.
    """
    data_dir = tmp_path / "_clipsync_data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "HISTORY_FILE", data_dir / "clipsync_history.json", raising=False)
    for name, filename in (
        ("SETTINGS_FILE", "settings.json"),
        ("LOG_FILE", "clipsync.log"),
    ):
        if hasattr(config, name):
            monkeypatch.setattr(config, name, data_dir / filename, raising=False)
    for name in ("APP_DATA_DIR", "SYNC_FOLDER"):
        if hasattr(config, name):
            monkeypatch.setattr(config, name, data_dir / name.lower(), raising=False)
    keyring = _MemoryKeyring()
    monkeypatch.setattr(config, "_keyring_backend", lambda: keyring)
    return data_dir
