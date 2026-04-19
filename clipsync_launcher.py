"""PyInstaller entry point with UI subprocess dispatch.

The frozen bundle's sys.executable points at the ClipSync binary
itself, not a Python interpreter, so the UIController cannot spawn
windows via `python -m clipsync.ui <name>`. Instead the main process
re-invokes its own binary with `ui <name>` as args; this launcher
routes those calls into the UI child entry.

Source runs via `python -m clipsync` still work unchanged (that path
hits clipsync/__main__.py, not this file).
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "ui":
        from clipsync.ui import _run_child

        window_name = sys.argv[2] if len(sys.argv) >= 3 else ""
        sys.exit(_run_child(window_name))
    else:
        from clipsync.main import main

        sys.exit(main())
