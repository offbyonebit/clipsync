# ClipSync

Peer-to-peer clipboard sync for Windows, macOS, and Linux. Runs in the
system tray. Uses [Syncthing](https://syncthing.net) as the transport,
so there's no account, no cloud, and no central server — just devices
talking directly to each other.

## How it works

- The app bundles a Syncthing binary, downloaded on first run.
- It creates a dedicated folder (`~/Library/Application Support/ClipSync/sync`
  on macOS, `%APPDATA%\ClipSync\sync` on Windows, `~/.config/clipsync/sync`
  on Linux) containing a single `clipboard.txt`.
- An **OUT** loop polls your local clipboard every 500 ms and writes any
  change to the file.
- An **IN** loop watches the file with `watchdog` and applies remote
  changes to your local clipboard.
- A loop-guard tracks the last synced value in both directions to prevent
  ping-pong.

## Install

```
pip install -r requirements.txt
python -m clipsync
```

Python 3.11+ is required. The Syncthing binary is downloaded automatically
from the official GitHub release on first run.

## Pairing

1. Click the tray icon -> **Add Device**.
2. On the second machine, do the same.
3. Either scan the QR code with the webcam, or paste the device ID shown
   below the QR.
4. Both sides will auto-accept and start syncing.

## Settings

The settings window exposes:

- Start on login (writes to HKCU Run / LaunchAgent / XDG autostart).
- Show notifications.
- Pause sync.
- Sync folder path.
- View Syncthing logs.
- Reset / unpair all devices.

## Files

| Module          | Purpose                                        |
|-----------------|------------------------------------------------|
| `config.py`     | Paths, settings persistence, logging.          |
| `syncthing.py`  | Download, configure, manage the Syncthing     |
|                 | subprocess; REST API client.                   |
| `clipboard.py`  | OUT polling + IN file watcher with loop guard. |
| `pairing.py`    | QR generation, webcam scanning, auto-accept.   |
| `ui.py`         | CustomTkinter pairing / devices / settings.    |
| `autostart.py`  | Cross-platform "run at login" toggle.          |
| `main.py`       | Orchestration, tray icon, lifecycle.           |
| `build.py`      | PyInstaller scaffold (stub).                   |

## License

MIT.
