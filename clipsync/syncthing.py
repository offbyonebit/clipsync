"""Syncthing lifecycle and REST API wrapper.

Handles: downloading the correct binary from GitHub releases on first run,
generating a home directory with cert/key/config.xml, patching config.xml
to expose a private REST API on 127.0.0.1:8385, running syncthing as a
managed subprocess with automatic restart, and exposing a small typed
client over the pieces of the REST API that the app cares about.

The Syncthing REST API lives on the GUI port, so we do not truly "disable"
the GUI. We bind it to loopback, set a random API key, and never open a
browser. The end user never sees a web UI.
"""

from __future__ import annotations

import io
import logging
import platform
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import requests

from . import config

log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 120
_API_TIMEOUT = 10
_STARTUP_WAIT = 30
_RESTART_DELAY = 10


class SyncthingError(RuntimeError):
    """Raised for any unrecoverable Syncthing failure surfaced to callers."""


def _platform_archive_info() -> tuple[str, str, str]:
    """Return (os_name, arch, extension) for the syncthing release asset."""
    system = platform.system()
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv7l": "arm",
        "i386": "386",
        "i686": "386",
    }
    arch = arch_map.get(machine)
    if arch is None:
        raise SyncthingError(f"Unsupported CPU architecture: {machine}")
    if system == "Windows":
        return "windows", arch, "zip"
    if system == "Darwin":
        return "macos", arch, "zip"
    if system == "Linux":
        return "linux", arch, "tar.gz"
    raise SyncthingError(f"Unsupported platform: {system}")


def _release_asset_url(version: str) -> str:
    os_name, arch, ext = _platform_archive_info()
    v = version if version.startswith("v") else f"v{version}"
    stem = f"syncthing-{os_name}-{arch}-{v}"
    return f"https://github.com/syncthing/syncthing/releases/download/{v}/{stem}.{ext}"


def _download(url: str) -> bytes:
    log.info("Downloading %s", url)
    req = Request(url, headers={"User-Agent": "ClipSync/1.0"})
    with urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        return resp.read()


def _extract_binary(data: bytes, ext: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_name = config.platform_binary_name()
    target = dest_dir / target_name

    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.namelist() if m.endswith(f"/{target_name}") or m.endswith(target_name)]
            if not members:
                raise SyncthingError("Syncthing binary not found in archive")
            with zf.open(members[0]) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tar_members = [
                m for m in tf.getmembers() if m.name.endswith(f"/{target_name}") or m.name.endswith(target_name)
            ]
            if not tar_members:
                raise SyncthingError("Syncthing binary not found in archive")
            extracted = tf.extractfile(tar_members[0])
            if extracted is None:
                raise SyncthingError("Failed to extract syncthing binary")
            with target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)

    if platform.system() != "Windows":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def ensure_binary(version: str = config.SYNCTHING_VERSION) -> Path:
    """Return the path to a working Syncthing binary, downloading if needed."""
    binary = config.syncthing_binary_path()
    if binary.exists() and binary.stat().st_size > 0:
        return binary
    _, _, ext = _platform_archive_info()
    url = _release_asset_url(version)
    try:
        data = _download(url)
    except URLError as exc:
        raise SyncthingError(f"Failed to download Syncthing: {exc}") from exc
    extracted = _extract_binary(data, ext, config.SYNCTHING_BIN_DIR)
    log.info("Installed syncthing binary at %s", extracted)
    return extracted


def _run_capture(args: list[str], timeout: int = 30) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")


def _generate_home(binary: Path, home: Path) -> None:
    """Run `syncthing generate --home=<home>` to produce cert/key/config."""
    home.mkdir(parents=True, exist_ok=True)
    if (home / "config.xml").exists():
        return
    log.info("Generating Syncthing home at %s", home)
    output = _run_capture([str(binary), "generate", f"--home={home}", "--no-default-folder"])
    if not (home / "config.xml").exists():
        raise SyncthingError(f"syncthing generate did not produce config.xml: {output}")


_LUHN_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _luhn32(s: str) -> str:
    """Compute Syncthing's Luhn mod-32 check character over a base32 string."""
    factor = 1
    total = 0
    n = 32
    for ch in s:
        codepoint = _LUHN_BASE32.index(ch)
        addend = factor * codepoint
        factor = 3 - factor
        addend = (addend // n) + (addend % n)
        total += addend
    check = (n - (total % n)) % n
    return _LUHN_BASE32[check]


def _device_id_from_cert(cert_pem_path: Path) -> str:
    """Compute the Syncthing device ID from a PEM-encoded certificate.

    The device ID is the SHA-256 of the DER-encoded cert, base32 encoded
    (no padding), split into four 13-char chunks each followed by a Luhn
    mod-32 check character, then chunked with hyphens every 7 chars.
    This matches Syncthing's own DeviceID.String() exactly, so it always
    returns the self device (unlike config.xml which after pairing holds
    multiple <device> entries with no reliable self marker).
    """
    import base64
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(cert_pem_path.read_bytes())
    der = cert.public_bytes(Encoding.DER)
    digest = hashlib.sha256(der).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    # 256 bits -> 52 base32 chars. Split into 4 x 13, append Luhn check on each.
    if len(b32) != 52:
        raise SyncthingError(f"Unexpected base32 length {len(b32)} for device id")
    chunks = [b32[i : i + 13] for i in range(0, 52, 13)]
    with_checks = "".join(c + _luhn32(c) for c in chunks)  # 56 chars
    return "-".join(with_checks[i : i + 7] for i in range(0, 56, 7))


def _read_device_id(binary: Path, home: Path) -> str:
    """Return our own device ID, derived from cert.pem like Syncthing does.

    Syncthing's device ID is the SHA-256 of its certificate, Luhn-
    checksummed and hyphen-chunked. Deriving from the cert is the only
    reliable way: config.xml after pairing holds multiple <device>
    entries with no distinguishable self marker, and --device-id isn't
    supported across all Syncthing versions.
    """
    cert_path = home / "cert.pem"
    if cert_path.exists():
        try:
            return _device_id_from_cert(cert_path)
        except Exception:
            log.exception("Failed to derive device ID from cert.pem, falling back to config.xml")

    # Fallback for anyone without cert.pem (shouldn't happen post-generate).
    config_path = home / "config.xml"
    tree = ET.parse(config_path)
    root = tree.getroot()
    for device in root.findall("device"):
        did = device.get("id", "")
        if did and "-" in did and len(did) >= 50:
            return did
    raise SyncthingError("Could not determine device ID")


def _xml_set(element: ET.Element, tag: str, text: str) -> ET.Element:
    child = element.find(tag)
    if child is None:
        child = ET.SubElement(element, tag)
    child.text = text
    return child


def _patch_config(home: Path, api_key: str, folder_path: Path, device_id: str) -> None:
    """Rewrite config.xml to reflect our desired GUI port, API key, folder."""
    config_path = home / "config.xml"
    tree = ET.parse(config_path)
    root = tree.getroot()

    gui = root.find("gui")
    if gui is None:
        gui = ET.SubElement(root, "gui")
    gui.set("enabled", "true")
    gui.set("tls", "false")
    _xml_set(gui, "address", f"{config.SYNCTHING_API_HOST}:{config.SYNCTHING_API_PORT}")
    _xml_set(gui, "apikey", api_key)
    _xml_set(gui, "theme", "default")

    for folder in list(root.findall("folder")):
        root.remove(folder)

    folder = ET.SubElement(root, "folder")
    folder.set("id", config.CLIPBOARD_FOLDER_ID)
    folder.set("label", "ClipSync")
    folder.set("path", str(folder_path))
    folder.set("type", "sendreceive")
    folder.set("rescanIntervalS", "10")
    folder.set("fsWatcherEnabled", "true")
    folder.set("fsWatcherDelayS", "1")
    folder.set("ignorePerms", "false")
    folder.set("autoNormalize", "true")
    fdev = ET.SubElement(folder, "device")
    fdev.set("id", device_id)
    fdev.set("introducedBy", "")

    options = root.find("options")
    if options is None:
        options = ET.SubElement(root, "options")
    _xml_set(options, "startBrowser", "false")
    _xml_set(options, "urAccepted", "-1")
    _xml_set(options, "crashReportingEnabled", "false")

    tree.write(config_path, encoding="utf-8", xml_declaration=True)


def prepare_home(binary: Path, settings: config.Settings) -> str:
    """Ensure a ready-to-use Syncthing home. Returns our device ID."""
    config.ensure_directories()
    _generate_home(binary, config.SYNCTHING_HOME)
    device_id = _read_device_id(binary, config.SYNCTHING_HOME)
    api_key = settings.get("api_key") or uuid.uuid4().hex
    settings.set("api_key", api_key)
    folder_path = Path(settings.get("sync_folder") or config.SYNC_FOLDER)
    folder_path.mkdir(parents=True, exist_ok=True)
    _patch_config(config.SYNCTHING_HOME, api_key, folder_path, device_id)
    return device_id


class SyncthingClient:
    """Thin wrapper over the Syncthing REST API used by the app."""

    def __init__(self, api_key: str, base_url: str = config.SYNCTHING_API_URL) -> None:
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = api_key

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _get(self, path: str) -> Any:
        resp = self._session.get(self._url(path), timeout=_API_TIMEOUT)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None

    def _post(self, path: str, payload: Any = None) -> Any:
        resp = self._session.post(self._url(path), json=payload, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return resp.text
        return None

    def _put(self, path: str, payload: Any) -> Any:
        resp = self._session.put(self._url(path), json=payload, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return resp.text
        return None

    def ping(self) -> bool:
        try:
            self._get("/rest/system/ping")
            return True
        except requests.RequestException:
            return False

    def wait_until_ready(self, timeout: float = _STARTUP_WAIT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ping():
                return True
            time.sleep(0.5)
        return False

    def get_device_id(self) -> str:
        status = self._get("/rest/system/status")
        return status["myID"]

    def get_config(self) -> dict[str, Any]:
        return self._get("/rest/config")

    def get_devices(self) -> list[dict[str, Any]]:
        return self._get("/rest/config/devices") or []

    def get_folders(self) -> list[dict[str, Any]]:
        return self._get("/rest/config/folders") or []

    def get_pending_devices(self) -> dict[str, dict[str, Any]]:
        try:
            data = self._get("/rest/cluster/pending/devices")
        except requests.HTTPError:
            return {}
        return data or {}

    def get_discovered_devices(self) -> dict[str, list[str]]:
        """Devices seen via local broadcast + global discovery, keyed by ID."""
        try:
            data = self._get("/rest/system/discovery")
        except requests.RequestException:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: list(v.get("addresses") or []) if isinstance(v, dict) else list(v or []) for k, v in data.items()}

    def get_connections(self) -> dict[str, Any]:
        return self._get("/rest/system/connections") or {}

    def add_device(self, device_id: str, name: str = "") -> None:
        """Idempotently add a remote device to our config."""
        devices = self.get_devices()
        if any(d.get("deviceID") == device_id for d in devices):
            return
        payload = {
            "deviceID": device_id,
            "name": name or device_id[:7],
            "addresses": ["dynamic"],
            "compression": "metadata",
            "introducer": False,
            "paused": False,
            "autoAcceptFolders": True,
        }
        self._post("/rest/config/devices", payload)

    def share_folder_with_device(self, device_id: str, folder_id: str = config.CLIPBOARD_FOLDER_ID) -> None:
        folders = self.get_folders()
        target = next((f for f in folders if f.get("id") == folder_id), None)
        if target is None:
            raise SyncthingError(f"Folder {folder_id!r} not found in config")
        folder_devices = target.get("devices") or []
        if any(d.get("deviceID") == device_id for d in folder_devices):
            return
        folder_devices.append({"deviceID": device_id, "introducedBy": ""})
        target["devices"] = folder_devices
        self._put(f"/rest/config/folders/{folder_id}", target)

    def rename_device(self, device_id: str, new_name: str) -> None:
        """Update the display name of a configured device."""
        devices = self.get_devices()
        target = next((d for d in devices if d.get("deviceID") == device_id), None)
        if target is None:
            raise SyncthingError(f"Device {device_id!r} not in config")
        target["name"] = new_name
        self._put(f"/rest/config/devices/{device_id}", target)

    def remove_device(self, device_id: str) -> None:
        try:
            self._session.delete(self._url(f"/rest/config/devices/{device_id}"), timeout=_API_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Failed to remove device %s: %s", device_id, exc)

    def set_folder_paused(self, paused: bool, folder_id: str = config.CLIPBOARD_FOLDER_ID) -> None:
        folders = self.get_folders()
        target = next((f for f in folders if f.get("id") == folder_id), None)
        if target is None:
            return
        if bool(target.get("paused")) == paused:
            return
        target["paused"] = paused
        self._put(f"/rest/config/folders/{folder_id}", target)

    def connected_devices(self) -> list[dict[str, Any]]:
        """Return a list of {deviceID, name, connected, address} for paired devices."""
        devices = self.get_devices()
        my_id = self.get_device_id()
        connections = self.get_connections().get("connections") or {}
        out: list[dict[str, Any]] = []
        for d in devices:
            did = d.get("deviceID")
            if not did or did == my_id:
                continue
            conn = connections.get(did) or {}
            out.append(
                {
                    "deviceID": did,
                    "name": d.get("name") or did[:7],
                    "connected": bool(conn.get("connected")),
                    "address": conn.get("address", ""),
                }
            )
        return out


class SyncthingService:
    """Owns the syncthing subprocess, restarts it if it exits unexpectedly."""

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._binary: Path | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._device_id: str = ""
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._lock = threading.Lock()
        self.client: SyncthingClient | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    def start(self) -> None:
        config.ensure_directories()
        self._binary = ensure_binary()
        self._device_id = prepare_home(self._binary, self._settings)
        self.client = SyncthingClient(self._settings.get("api_key"))
        self._spawn()
        if not self.client.wait_until_ready():
            raise SyncthingError("Syncthing did not become ready in time")
        log.info("Syncthing ready (device %s)", self._device_id)
        self._stop.clear()
        self._monitor = threading.Thread(target=self._watch, name="syncthing-monitor", daemon=True)
        self._monitor.start()

    def _spawn(self) -> None:
        assert self._binary is not None
        args = [
            str(self._binary),
            "serve",
            f"--home={config.SYNCTHING_HOME}",
            "--no-browser",
            "--no-restart",
        ]
        log.info("Spawning: %s", " ".join(args))
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self._lock:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )

    def _watch(self) -> None:
        while not self._stop.is_set():
            proc = self._proc
            if proc is None:
                break
            rc = proc.poll()
            if rc is None:
                time.sleep(1.0)
                continue
            if self._stop.is_set():
                break
            log.error("Syncthing exited with code %s, restarting in %ss", rc, _RESTART_DELAY)
            if self._stop.wait(_RESTART_DELAY):
                break
            try:
                self._spawn()
                if self.client is not None:
                    self.client.wait_until_ready()
            except Exception:
                log.exception("Failed to restart Syncthing")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc and proc.poll() is None:
            log.info("Stopping Syncthing")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                log.exception("Error stopping Syncthing")
        if self._monitor and self._monitor.is_alive():
            self._monitor.join(timeout=3)
