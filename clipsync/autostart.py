"""Cross-platform "start on login" toggle.

Windows: HKCU Run registry key pointing at the current executable.
macOS:   ~/Library/LaunchAgents/com.clipsync.plist (loaded on login).
Linux:   ~/.config/autostart/clipsync.desktop (XDG Autostart).

Writes are idempotent; disabling removes the artifact.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_LABEL = "ClipSync"
_BUNDLE_ID = "com.clipsync"


def _launch_command() -> list[str]:
    """Return the argv needed to launch the app at login."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "clipsync"]


def _windows_set(enabled: bool) -> None:
    import winreg  # type: ignore

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            cmd = " ".join(f'"{a}"' for a in _launch_command())
            winreg.SetValueEx(key, _LABEL, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, _LABEL)
            except FileNotFoundError:
                pass


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_BUNDLE_ID}.plist"


def _macos_set(enabled: bool) -> None:
    path = _macos_plist_path()
    if not enabled:
        if path.exists():
            path.unlink()
        return
    argv = _launch_command()
    args_xml = "\n".join(f"        <string>{a}</string>" for a in argv)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key><string>{_BUNDLE_ID}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        "    <key>KeepAlive</key><false/>\n"
        "</dict>\n"
        "</plist>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist, encoding="utf-8")


def _linux_desktop_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "autostart" / "clipsync.desktop"


def _linux_set(enabled: bool) -> None:
    path = _linux_desktop_path()
    if not enabled:
        if path.exists():
            path.unlink()
        return
    cmd = " ".join(shlex.quote(a) for a in _launch_command())
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_LABEL}\n"
        f"Exec={cmd}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=false\n"
        "Terminal=false\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry, encoding="utf-8")


def set_autostart(enabled: bool) -> None:
    """Toggle start-on-login. Swallows and logs per-platform failures."""
    system = platform.system()
    try:
        if system == "Windows":
            _windows_set(enabled)
        elif system == "Darwin":
            _macos_set(enabled)
        elif system == "Linux":
            _linux_set(enabled)
        else:
            log.warning("Autostart not supported on %s", system)
    except Exception:
        log.exception("Failed to set autostart=%s", enabled)


def is_autostart_enabled() -> bool:
    system = platform.system()
    try:
        if system == "Windows":
            import winreg  # type: ignore

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                try:
                    winreg.QueryValueEx(key, _LABEL)
                    return True
                except FileNotFoundError:
                    return False
        if system == "Darwin":
            return _macos_plist_path().exists()
        if system == "Linux":
            return _linux_desktop_path().exists()
    except Exception:
        log.exception("Failed to query autostart state")
    return False
