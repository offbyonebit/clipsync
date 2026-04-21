# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install deps
pip install -r requirements.txt

# Run (dev mode)
python -m clipsync

# Build (PyInstaller, output to dist/)
python -m clipsync.build

# Lint
ruff check clipsync/ tests/
ruff format clipsync/ tests/

# Type check (ideally on Windows so winreg resolves natively)
mypy clipsync/

# Tests
pytest tests/
pytest tests/test_update.py::test_is_newer   # single test
```

Ruff: line-length=120, target py311. Mypy: strict mode.

## Architecture

ClipSync syncs the system clipboard across platforms by writing to a single shared file (`clipboard.txt` or `clipboard.png`) inside a Syncthing folder. Syncthing handles all networking — peer discovery, NAT traversal, relay fallback, and TLS.

**Orchestration (`main.py` → `ClipSyncApp`):**
1. Launch Syncthing subprocess and wait for REST API readiness
2. Start worker threads: clipboard sync (OUT+IN loops), pending device watcher, log mirror
3. Create tray icon on main thread (required on macOS)
4. Shutdown reverses this order — Syncthing stops last to flush pending writes

**Key modules:**

- `config.py` — platform-specific paths, JSON-backed `Settings` class with auto-reload on file change, all shared constants
- `syncthing.py` — downloads pinned Syncthing binary (`SYNCTHING_VERSION = "v1.27.10"`), generates config, patches it to expose private API on `127.0.0.1:8385`, manages subprocess lifecycle, kills orphans on restart
- `clipboard.py` — OUT loop polls system clipboard every 0.5s and writes changes to the sync file; IN loop uses a watchdog file observer to apply remote changes back to the system clipboard. A shared-last-value guard prevents ping-pong. Optional Fernet encryption (AES-128-CBC + HMAC-SHA256, PBKDF2 key derivation) is signalled by a `CSENC` magic header prefix on the ciphertext.
- `pairing.py` — QR code generation for device IDs, webcam scanning via OpenCV, background poller that auto-accepts or notifies on pending Syncthing device requests
- `ui.py` — CustomTkinter windows are spawned as **subprocesses**. The child process emits JSON events to stdout; the parent `UIController` reads and dispatches them. This keeps the tray (main thread) responsive on all platforms.
- `autostart.py` — Windows: `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run`; macOS: LaunchAgent plist; Linux: XDG Autostart `.desktop` file
- `update.py` — polls GitHub Releases API, compares semantic versions, opens download URL in browser (no auto-install)
- `debug.py` — `LogMirror` thread copies the last 20 KB of the local `clipsync.log` into `{sync_folder}/debug/{hostname}.log` every 10s, making all peers' logs accessible via Syncthing
- `single_instance.py` — OS-level exclusive file lock (`msvcrt` on Windows, `fcntl` on POSIX) to prevent two instances competing for Syncthing's database lock

## Release

Triggered by pushing a `v*` tag (or manually via workflow dispatch). GitHub Actions (`release.yml`) runs a build matrix on **self-hosted** runners:

| Runner label | Artifact |
|---|---|
| `[self-hosted, macos]` | `ClipSync-macos-arm64.zip` |
| `[self-hosted, windows]` | `ClipSync-windows-x86_64.zip` |
| `[self-hosted, linux]` | `ClipSync-linux-x86_64.tar.gz` |

Each build runs `python -m clipsync.build` (PyInstaller with platform-specific flags) then archives the output. A final job creates the GitHub Release and attaches all artifacts. Runners must be registered and labeled before the workflow can execute.

CI (`ruff`, `mypy`, `pytest`) is run locally — there are no CI workflow files in the repo.
