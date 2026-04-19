"""Image clipboard sync tests.

Mirrors the structure of test_mac_windows_sync.py but for image content.
Platform clipboard access is replaced by fake read/write methods so the
tests run on any OS without a display.
"""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image
from watchdog.observers.polling import PollingObserver

from clipsync import clipboard as clipboard_module
from clipsync import config
from clipsync.clipboard import ClipboardSync

POLL = 0.05


def _make_png(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Return a minimal 1x1 PNG of the given RGB color."""
    img = Image.new("RGB", (1, 1), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


FAKE_PNG_RED = _make_png((255, 0, 0))
FAKE_PNG_BLUE = _make_png((0, 0, 255))


def _install_fakes(sync: ClipboardSync) -> tuple[dict, dict]:
    """Attach fake text and image clipboards to a ClipboardSync instance.
    Returns (text_state, image_state) dicts with 'value'/'image' keys."""
    text_state: dict = {"value": ""}
    image_state: dict = {"image": None}

    sync._read_clipboard = lambda: text_state["value"] or None  # type: ignore[method-assign]
    sync._write_clipboard = lambda v: text_state.__setitem__("value", v) or True  # type: ignore[method-assign]
    sync._read_clipboard_image = lambda: image_state["image"]  # type: ignore[method-assign]
    sync._write_clipboard_image = lambda b: image_state.__setitem__("image", b) or True  # type: ignore[method-assign]

    return text_state, image_state


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def two_sided(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLIPBOARD_POLL_INTERVAL", POLL)
    monkeypatch.setattr(clipboard_module, "Observer", PollingObserver)

    sync_folder = tmp_path / "shared_sync"
    sync_folder.mkdir()

    settings_a = config.Settings(path=tmp_path / "a_settings.json")
    settings_a.set("sync_folder", str(sync_folder))
    settings_b = config.Settings(path=tmp_path / "b_settings.json")
    settings_b.set("sync_folder", str(sync_folder))

    a = ClipboardSync(settings_a)
    b = ClipboardSync(settings_b)

    txt_a, img_a = _install_fakes(a)
    txt_b, img_b = _install_fakes(b)

    a.start()
    b.start()

    yield a, b, txt_a, img_a, txt_b, img_b

    a.stop()
    b.stop()


def test_image_syncs_a_to_b(two_sided) -> None:
    _, _, _, img_a, _, img_b = two_sided
    img_a["image"] = FAKE_PNG_RED
    assert _wait_for(lambda: img_b["image"] == FAKE_PNG_RED), (
        f"Image did not sync to B; got {img_b['image']!r}"
    )


def test_image_syncs_b_to_a(two_sided) -> None:
    _, _, _, img_a, _, img_b = two_sided
    img_b["image"] = FAKE_PNG_BLUE
    assert _wait_for(lambda: img_a["image"] == FAKE_PNG_BLUE), (
        f"Image did not sync to A; got {img_a['image']!r}"
    )


def test_image_does_not_oscillate(two_sided) -> None:
    """An image sync must settle after one round-trip, not bounce forever."""
    a, b, _, img_a, _, img_b = two_sided
    img_a["image"] = FAKE_PNG_RED

    assert _wait_for(lambda: img_b["image"] == FAKE_PNG_RED)

    write_count = {"n": 0}
    original_write = b._write_clipboard_image

    def counting_write(png_bytes: bytes) -> bool:
        write_count["n"] += 1
        return original_write(png_bytes)

    b._write_clipboard_image = counting_write  # type: ignore[method-assign]

    time.sleep(POLL * 15)
    assert write_count["n"] == 0, f"Image sync oscillated {write_count['n']} extra time(s)"


def test_text_still_syncs_when_no_image(two_sided) -> None:
    """Regression: text sync must work when image clipboard always returns None."""
    _, _, txt_a, _, txt_b, _ = two_sided
    txt_a["value"] = "hello from a"
    assert _wait_for(lambda: txt_b["value"] == "hello from a"), (
        f"Text did not sync; got {txt_b['value']!r}"
    )


def test_image_sync_with_encryption(two_sided) -> None:
    a, b, _, img_a, _, img_b = two_sided
    a._settings.set("encryption_passphrase", "shared-secret")
    b._settings.set("encryption_passphrase", "shared-secret")
    time.sleep(POLL * 3)

    img_a["image"] = FAKE_PNG_RED
    assert _wait_for(lambda: img_b["image"] == FAKE_PNG_RED), (
        f"Encrypted image did not sync; got {img_b['image']!r}"
    )


def test_encrypted_image_mismatched_passphrase(two_sided) -> None:
    """B must not apply an image it cannot decrypt."""
    a, b, _, img_a, _, img_b = two_sided
    a._settings.set("encryption_passphrase", "pass-a")
    b._settings.set("encryption_passphrase", "pass-b")
    time.sleep(POLL * 3)

    img_a["image"] = FAKE_PNG_RED
    time.sleep(POLL * 20)
    assert img_b["image"] is None, (
        f"B applied image despite passphrase mismatch; got {img_b['image']!r}"
    )
