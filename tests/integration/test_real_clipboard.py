"""Integration tests that hit the real system clipboard.

Run locally with:  pytest tests/integration/ -v
Requires: X11 (DISPLAY set) with xclip, or Wayland with wl-paste/wl-copy

These tests are gitignored so they never run in CI.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap

import pytest
from PIL import Image

from clipsync.clipboard import (
    _read_image_from_system_clipboard,
    _write_image_to_system_clipboard,
)

pytestmark = pytest.mark.integration

needs_display = pytest.mark.skipif(
    not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"),
    reason="Requires a display server (X11 or Wayland)",
)

needs_xclip = pytest.mark.skipif(
    sys.platform != "linux" and sys.platform != "darwin",
    reason="Local Linux xclip tests only",
)


def _make_png(color: tuple[int, int, int] = (128, 64, 255)) -> bytes:
    img = Image.new("RGB", (3, 2), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG_A = _make_png((255, 0, 0))
PNG_B = _make_png((0, 128, 0))


def _clipboard_has_image() -> bool:
    """Check if there's currently an image on the clipboard via xclip."""
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0 and bool(result.stdout)
    except Exception:
        return False


def _write_image_via_xclip(png_bytes: bytes) -> None:
    subprocess.run(
        ["xclip", "-selection", "clipboard", "-t", "image/png"],
        input=png_bytes, capture_output=True, timeout=3, check=True,
    )


def _read_image_via_xclip() -> bytes:
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        capture_output=True, timeout=3, check=True,
    )
    return result.stdout


def _write_text_via_xclip(text: str) -> None:
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text.encode(), capture_output=True, timeout=3, check=True,
    )


def _read_text_via_xclip() -> str:
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-o"],
        capture_output=True, timeout=3, check=True,
    )
    return result.stdout.decode("utf-8")


# ── Image clipboard ────────────────────────────────────────────────────


@needs_display
class TestReadImageFromSystemClipboard:
    def test_reads_png_when_image_present(self):
        _write_image_via_xclip(PNG_A)
        result = _read_image_from_system_clipboard()
        assert result is not None, "Should detect image on clipboard"
        assert result[:4] == b"\x89PNG", "Should return valid PNG"

    def test_returns_none_when_no_image(self):
        _write_text_via_xclip("no image here")
        result = _read_image_from_system_clipboard()
        assert result is None, f"Should return None for text-only clipboard, got {len(result) if result else 0} bytes"

    def test_readback_matches_original(self):
        _write_image_via_xclip(PNG_B)
        result = _read_image_from_system_clipboard()
        assert result == PNG_B, f"Readback mismatch: {len(result) if result else 0} vs {len(PNG_B)} bytes"


@needs_display
class TestWriteImageToSystemClipboard:
    def test_writes_png_and_readable_by_xclip(self):
        ok = _write_image_to_system_clipboard(PNG_A)
        assert ok, "_write_image_to_system_clipboard should return True"
        assert _clipboard_has_image(), "xclip should see image after write"

    def test_round_trip(self):
        _write_image_to_system_clipboard(PNG_B)
        readback = _read_image_via_xclip()
        # xclip may return additional bytes (PNG chunk ordering can vary),
        # but should at least be a valid PNG of similar size
        assert readback[:4] == b"\x89PNG"
        assert abs(len(readback) - len(PNG_B)) < 100, (
            f"Round-trip size mismatch: {len(readback)} vs {len(PNG_B)}"
        )


# ── Text clipboard (via pyperclip) ─────────────────────────────────────


@needs_display
class TestTextClipboardViaPyperclip:
    def test_round_trip_pyperclip(self):
        import pyperclip

        pyperclip.copy("hello integration test")
        value = pyperclip.paste()
        assert value == "hello integration test", f"pyperclip round-trip failed: {value!r}"

    def test_multiline_pyperclip(self):
        import pyperclip

        text = "line one\nline two\nline three"
        pyperclip.copy(text)
        # Linux preserves LF; Windows would convert to CRLF
        value = pyperclip.paste()
        assert value.replace("\r\n", "\n") == text, f"Multiline mismatch: {value!r}"

    def test_large_text_pyperclip(self):
        import pyperclip

        large = "abcdefghij" * 1000
        pyperclip.copy(large)
        value = pyperclip.paste()
        assert value == large, f"Large text lost data: {len(value)} vs {len(large)}"


# ── Full sync round-trip (ClipboardSync with real clipboard) ───────────


@needs_display
class TestRealClipboardSync:
    """End-to-end: write to real clipboard, verify sync file appears."""

    def test_text_sync_writes_file(self, tmp_path):
        import time
        from clipsync import config
        from clipsync.clipboard import ClipboardSync

        import pyperclip

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        settings = config.Settings(path=tmp_path / "settings.json")
        settings.set("sync_folder", str(sync_folder))

        sync = ClipboardSync(settings)
        sync.start()
        try:
            pyperclip.copy("real clipboard test")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                clipboard_file = sync_folder / config.CLIPBOARD_FILENAME
                if clipboard_file.exists():
                    content = clipboard_file.read_text()
                    if "real clipboard test" in content:
                        break
                time.sleep(0.1)
            else:
                files = list(sync_folder.iterdir())
                pytest.fail(f"Sync file not written; folder contents: {files}")
        finally:
            sync.stop()

    def test_text_sync_reads_file(self, tmp_path):
        import time
        from clipsync import config
        from clipsync.clipboard import ClipboardSync

        import pyperclip

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        settings = config.Settings(path=tmp_path / "settings.json")
        settings.set("sync_folder", str(sync_folder))

        clipboard_file = sync_folder / config.CLIPBOARD_FILENAME
        clipboard_file.write_text("from file to clipboard")

        sync = ClipboardSync(settings)
        sync.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = pyperclip.paste()
                if "from file to clipboard" in str(current):
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"Clipboard not updated from file; current: {pyperclip.paste()!r}")
        finally:
            sync.stop()
