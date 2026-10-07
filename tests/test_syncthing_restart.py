"""Unit tests for Syncthing restart behavior."""

from clipsync import syncthing


def test_restart_delay_doubles_and_is_bounded() -> None:
    delay = syncthing._RESTART_DELAY
    assert syncthing._next_restart_delay(delay) == delay * 2
    assert syncthing._next_restart_delay(syncthing._RESTART_DELAY_MAX // 2) == syncthing._RESTART_DELAY_MAX
    assert syncthing._next_restart_delay(syncthing._RESTART_DELAY_MAX) == syncthing._RESTART_DELAY_MAX


class _CrashedProcess:
    def poll(self) -> int:
        return 1


class _StopAfterThreeWaits:
    def __init__(self) -> None:
        self.delays: list[int] = []

    def is_set(self) -> bool:
        return False

    def wait(self, delay: int) -> bool:
        self.delays.append(delay)
        return len(self.delays) == 3


class _ReadyClient:
    def wait_until_ready(self) -> bool:
        return True


def test_watch_resets_delay_after_ready_restart_then_backs_off(tmp_path, monkeypatch) -> None:
    """A ready restart resets the next crash to the short retry delay."""
    service = syncthing.SyncthingService(syncthing.config.Settings(path=tmp_path / "settings.json"))
    stop = _StopAfterThreeWaits()
    service._proc = _CrashedProcess()  # type: ignore[assignment]
    service._stop = stop  # type: ignore[assignment]
    service.client = _ReadyClient()  # type: ignore[assignment]

    attempts = 0

    def spawn() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("still unavailable")

    monkeypatch.setattr(service, "_spawn", spawn)

    service._watch()

    assert attempts == 2
    assert stop.delays == [10, 10, 20]
