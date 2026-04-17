"""PyInstaller build scaffold.

Not wired up yet. Intended invocation once the flags are finalized:

    python -m clipsync.build

Notes / TODOs for a real build:

- Windows: `pyinstaller --noconsole --icon=assets/icon.ico \
    --add-data "assets;assets" -n ClipSync clipsync/__main__.py`
- macOS:   `pyinstaller --windowed --icon=assets/icon.icns \
    --add-data "assets:assets" -n ClipSync clipsync/__main__.py`
    Then wrap the resulting .app for notarization.
- Linux:   `pyinstaller --add-data "assets:assets" \
    -n clipsync clipsync/__main__.py`

The Syncthing binary is downloaded on first run rather than bundled so the
build output stays small; the pipeline could alternatively bundle it via
`--add-binary` for offline installs.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("build.py is a stub. See this module's docstring for flags.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
