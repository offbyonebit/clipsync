"""Tests for cross-platform start-on-login helpers."""

from __future__ import annotations

import plistlib
import sys

import pytest

from clipsync import autostart


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_macos_plist_escapes_xml_special_characters(tmp_path, monkeypatch) -> None:
    """Arguments containing &, <, > must be written as a valid plist."""
    plist_path = tmp_path / "com.clipsync.plist"
    monkeypatch.setattr(autostart, "_macos_plist_path", lambda: plist_path)

    # Inject a malicious-looking argument that would break string concatenation.
    monkeypatch.setattr(autostart, "_launch_command", lambda: ["clipsync", "--args", "foo & bar <baz>"])

    autostart._macos_set(True)
    assert plist_path.exists()
    with plist_path.open("rb") as fh:
        loaded = plistlib.load(fh)
    assert loaded["ProgramArguments"] == ["clipsync", "--args", "foo & bar <baz>"]
