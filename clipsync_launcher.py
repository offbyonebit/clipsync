"""PyInstaller entry point.

PyInstaller runs its entry script as `__main__` with no parent package,
which breaks the relative `from .main import main` in
clipsync/__main__.py. This wrapper uses absolute imports so both the
frozen bundle and `python -m clipsync` keep working.
"""

from __future__ import annotations

import sys

from clipsync.main import main

if __name__ == "__main__":
    sys.exit(main())
