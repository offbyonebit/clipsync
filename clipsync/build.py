"""PyInstaller build entry point.

Invoke as `python -m clipsync.build` on any supported OS; the flags
are picked per platform so CI can use the same one-liner on every
runner.

Output goes to `dist/ClipSync` (or `dist/clipsync` on Linux), which CI
then zips / tars and attaches to the GitHub release.

The Syncthing binary is intentionally NOT bundled. It is ~30 MB per
platform and clipsync.syncthing already downloads the correct release
asset on first run, which keeps the artifact small and always
up-to-date with pinned Syncthing versions.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "clipsync" / "assets"
# Use the absolute-import wrapper instead of clipsync/__main__.py so the
# bundle does not choke on `from .main import main`. PyInstaller runs its
# entry script as the top-level __main__, which has no parent package.
ENTRY_POINT = PROJECT_ROOT / "clipsync_launcher.py"


def _data_arg() -> str:
    """PyInstaller --add-data uses `:` on POSIX, `;` on Windows."""
    sep = ";" if platform.system() == "Windows" else ":"
    return f"{ASSETS_DIR}{sep}assets"


def _common_args(name: str) -> list[str]:
    return [
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
        "--add-data",
        _data_arg(),
        # UI windows are respawned via `python -m clipsync.ui`, so the
        # bundle must include the ui module even though __main__ does
        # not import it directly in every code path.
        "--collect-submodules",
        "clipsync",
        # customtkinter ships TTF files that don't auto-detect.
        "--collect-data",
        "customtkinter",
        str(ENTRY_POINT),
    ]


def _platform_args() -> list[str]:
    system = platform.system()
    if system == "Windows":
        return ["--noconsole"]
    if system == "Darwin":
        # AppKit / Foundation are imported lazily inside image-write
        # functions, which PyInstaller's static analysis sometimes
        # misses. Declare them explicitly so the bundle includes pyobjc.
        return [
            "--windowed",
            "--hidden-import",
            "AppKit",
            "--hidden-import",
            "Foundation",
        ]
    return []  # Linux: keep console; users typically launch via tray anyway


def main() -> int:
    if shutil.which("pyinstaller") is None:
        print("pyinstaller not installed. Run: pip install pyinstaller", file=sys.stderr)
        return 1

    name = "ClipSync" if platform.system() != "Linux" else "clipsync"
    args = _common_args(name) + _platform_args()
    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=PROJECT_ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
