"""Device pairing primitives.

- QR code generation (PIL image) for our device ID
- Webcam scanning via OpenCV's built-in QRCodeDetector
- Background poller that auto-accepts pending device requests and auto-
  shares the clipsync folder with any newly-known device
- A small validator for pasted device IDs
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable

import qrcode
from PIL import Image

from . import config
from .syncthing import SyncthingClient

log = logging.getLogger(__name__)

_DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")


def normalize_device_id(raw: str) -> str | None:
    """Strip whitespace, uppercase, and validate the device ID shape."""
    if not raw:
        return None
    candidate = raw.strip().upper()
    candidate = re.sub(r"\s+", "", candidate)
    if _DEVICE_ID_RE.match(candidate):
        return candidate
    return None


def generate_qr(device_id: str, box_size: int = 8, border: int = 2) -> Image.Image:
    """Return a PIL image of a QR code encoding the raw device ID."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(device_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img


class WebcamQRScanner:
    """Open the default webcam and invoke on_detected the first time it sees
    a valid device-ID QR code. Designed to run in a dedicated thread with a
    small Tk-hosted preview window driven by the caller's main loop."""

    def __init__(self, on_detected: Callable[[str], None]) -> None:
        self._on_detected = on_detected
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_callback: Callable[[object], None] | None = None

    def set_frame_callback(self, cb: Callable[[object], None]) -> None:
        """Register a callback that receives raw BGR frames for preview."""
        self._frame_callback = cb

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipsync-qr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            import cv2
        except ImportError:
            log.error("opencv-python not installed, cannot scan QR codes")
            return
        import platform

        system = platform.system()
        if system == "Windows":
            backend = cv2.CAP_DSHOW
        elif system == "Darwin":
            backend = cv2.CAP_AVFOUNDATION
        else:
            backend = cv2.CAP_V4L2
        cap = cv2.VideoCapture(0, backend)
        if not cap.isOpened():
            log.warning("Fast backend failed, retrying with default")
            cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            log.error("Could not open webcam")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        detector = cv2.QRCodeDetector()
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue
                if self._frame_callback is not None:
                    try:
                        self._frame_callback(frame)
                    except Exception:
                        log.exception("Frame callback raised")
                try:
                    data, _, _ = detector.detectAndDecode(frame)
                except cv2.error:
                    data = ""
                if data:
                    device_id = normalize_device_id(data)
                    if device_id:
                        self._on_detected(device_id)
                        return
                time.sleep(0.03)
        finally:
            cap.release()


class PendingDeviceWatcher:
    """Poll Syncthing for pending device requests and notify the app.

    When `auto_accept` is True the watcher falls back to the original
    zero-interaction behavior: incoming requests are paired immediately.
    When False (the default in 0.1.1+), the watcher only notifies the
    app about new pending requests so the user can accept or reject
    them from the tray; devices previously rejected are skipped."""

    def __init__(
        self,
        client: SyncthingClient,
        on_pending: Callable[[str, dict[str, object]], None] | None = None,
        on_accepted: Callable[[str], None] | None = None,
        is_rejected: Callable[[str], bool] | None = None,
        auto_accept: Callable[[], bool] | None = None,
        interval: float = config.PAIRING_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._on_pending = on_pending
        self._on_accepted = on_accepted
        self._is_rejected = is_rejected or (lambda _did: False)
        self._auto_accept = auto_accept or (lambda: False)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notified: set[str] = set()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipsync-pair-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def forget(self, device_id: str) -> None:
        """Allow the user to be re-notified about a device after it has been
        accepted or rejected. Called by the app after it records the outcome."""
        self._notified.discard(device_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("Error in pending device watcher")
            if self._stop.wait(self._interval):
                break

    def _tick(self) -> None:
        pending = self._client.get_pending_devices() or {}
        seen: set[str] = set()
        for device_id, info in pending.items():
            normalized = normalize_device_id(device_id)
            if not normalized:
                continue
            seen.add(normalized)
            if self._is_rejected(normalized):
                continue
            info_dict = info or {}
            if self._auto_accept():
                name = info_dict.get("name") or normalized[:7]
                try:
                    self._client.add_device(normalized, name=str(name))
                    self._client.share_folder_with_device(normalized)
                    log.info("Auto-accepted device %s", normalized)
                    if self._on_accepted is not None:
                        self._on_accepted(normalized)
                except Exception:
                    log.exception("Failed to auto-accept %s", normalized)
                continue
            if normalized in self._notified:
                continue
            self._notified.add(normalized)
            log.info("Pending device request from %s", normalized)
            if self._on_pending is not None:
                try:
                    self._on_pending(normalized, info_dict)
                except Exception:
                    log.exception("on_pending callback raised")
        # Drop notifications for device IDs no longer pending so that if
        # the same device tries again later we prompt the user again.
        self._notified &= seen


# Kept for backwards compat with older callers that imported the original name.
PendingDeviceAccepter = PendingDeviceWatcher


def pair_with_device(client: SyncthingClient, device_id: str, name: str = "") -> None:
    """Register a remote device and share the clipsync folder with it."""
    normalized = normalize_device_id(device_id)
    if not normalized:
        raise ValueError("Invalid device ID")
    client.add_device(normalized, name=name)
    client.share_folder_with_device(normalized)


def accept_pending_device(client: SyncthingClient, device_id: str, name: str = "") -> str:
    """Accept a pending incoming device. Returns the normalized device ID."""
    normalized = normalize_device_id(device_id)
    if not normalized:
        raise ValueError("Invalid device ID")
    client.add_device(normalized, name=name or normalized[:7])
    client.share_folder_with_device(normalized)
    return normalized
