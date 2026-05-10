# ClipSync

Peer-to-peer clipboard sync for Windows, macOS, and Linux. Copy on one machine,
paste on another. No account, no cloud, no central server. The data never
leaves your devices.

Verified working across Mac and Windows, across different networks.

## Why this exists

Every other cross-platform clipboard tool I found falls into one of three
buckets:

1. **Same-LAN only** - needs mDNS or a discovery broadcast, so it breaks the
   moment you're on a different network (or a corporate one that blocks
   multicast).
2. **Needs a signaling server** - even the "peer-to-peer" modes usually still
   need a relay or a server you host somewhere, which means one more thing to
   maintain and one more thing that can go down.
3. **Reinvented the networking stack** - custom libp2p, custom NAT traversal,
   custom encryption. Which means custom bugs.

The design choice here is to **reuse Syncthing** instead of inventing anything.
Syncthing has already solved peer discovery, NAT traversal, TLS with forward
secrecy, and relay fallback for millions of users. By treating a single file
(`clipboard.txt`) inside a shared Syncthing folder as the transport, you
inherit all of that for free, including cross-WAN sync that just works,
without Tailscale, without port forwarding, and without a VPN.

Two loops sit on top:

- **OUT** polls the local clipboard and writes changes to the file.
- **IN** watches the file with filesystem events and applies remote changes to
  the clipboard.

A shared-last-value guard prevents ping-pong when both sides see the same
write. Newlines are normalized to LF so Windows CRLF doesn't look like a real
change and retrigger sync.

## At-rest encryption

Syncthing already encrypts data in transit. For defense-in-depth, in case
someone gains read access to the sync folder on one device, you can set a
passphrase in Settings. Payloads are then encrypted with Fernet (AES-128-CBC +
HMAC-SHA256) using a PBKDF2-SHA256 key derivation, and prefixed with a `CSENC`
magic header so peers can detect ciphertext and refuse to paste it raw.

Every device in the group needs the same passphrase. A mismatch logs a clear
"decrypt failed (passphrase mismatch?)" message rather than silently pasting
ciphertext.

## Install

```
pip install -r requirements.txt
python -m clipsync
```

Python 3.11+. The correct Syncthing binary for your platform is downloaded on
first run from the official Syncthing GitHub release.

### Linux setup notes

`pystray` picks its tray backend at import time: PyGObject + AppIndicator if
available, XEmbed otherwise. KDE Plasma and several other modern desktops no
longer render XEmbed icons; they only speak `StatusNotifierItem`, which
AppIndicator provides. If the tray icon silently never appears, install the
system packages and make them visible to your venv.

**Debian / Ubuntu / Mint**
```
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 xclip
```

**Fedora**
```
sudo dnf install python3-gobject libayatana-appindicator-gtk3 xclip
```

**Arch**
```
sudo pacman -S python-gobject libayatana-appindicator xclip
```

If you use a virtualenv, set `include-system-site-packages = true` in its
`pyvenv.cfg` so the system-installed PyGObject is importable. `xclip` is the
X11 backend `pyperclip` uses for clipboard access; on Wayland install
`wl-clipboard` instead. GNOME users typically also need the
[AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/)
for the icon to actually show up.

## Pairing

1. Tray icon -> **Add Device**.
2. On the second machine, do the same.
3. Scan the QR code with the webcam, or paste the device ID shown below it.
4. Both sides auto-accept and start syncing.

## Settings

- Start on login (HKCU Run on Windows, LaunchAgent on macOS, XDG autostart on
  Linux).
- Show notifications.
- Pause sync.
- Sync folder path.
- Encryption passphrase.
- View Syncthing logs.
- Reset / unpair all devices.
- Check for updates (compares against the latest GitHub release and opens the
  download page, no auto-install, no phone-home on startup).

Settings changes from the UI take effect immediately; hand-editing
`settings.json` is also picked up, the file is watched for changes.

## How it compares

| Tool | Cross-WAN | No signaling server | E2E encrypted | Tray UI |
|---|---|---|---|---|
| **ClipSync** | yes (via Syncthing relays) | yes | yes | yes |
| p2p-clipboard | partial | yes | yes | yes |
| ClipCascade | yes | no (self-host required) | yes | yes |
| cross-clipboard | no (LAN mDNS) | yes | yes | no |
| macOS Universal Clipboard | yes (iCloud) | no | N/A | built-in |
| Windows Cloud Clipboard | yes | no (Microsoft account) | N/A | built-in |

## Project layout

| Module | Purpose |
|---|---|
| `config.py` | Paths, settings with on-disk watch, logging. |
| `syncthing.py` | Binary download, config generation, REST API client, subprocess lifecycle. |
| `clipboard.py` | OUT poll + IN watcher, loop guard, optional Fernet encryption. |
| `pairing.py` | QR generation, webcam scan, auto-accept of pending devices. |
| `ui.py` | CustomTkinter windows for pairing, devices, and settings. |
| `autostart.py` | Cross-platform "run at login" toggle. |
| `main.py` | Tray icon, orchestration, shutdown ordering. |

## Status

Working on Windows and macOS. Linux should work (the autostart and clipboard
code paths are present) but hasn't been as heavily exercised.

## License

MIT. See [LICENSE](LICENSE). You can fork, modify, and redistribute
(including commercially), but the copyright notice must travel with the
code.

## Support

This project is free and I don't ask for anything. If it's useful to you,
a star on the repo is appreciated, and if you want to follow along with
other things I'm building, you can find them under
[@offbyonebit](https://github.com/offbyonebit).

If you'd like to support development, you can [sponsor me on GitHub](https://github.com/sponsors/offbyonebit).
