"""CustomTkinter UI: pairing, devices, settings, logs windows.

Architecture: each window is opened in its own short-lived Python
subprocess. The parent process runs the tray icon on its main thread
(required on macOS 26 where AppKit must be initialized on the main
thread). Child processes speak to the parent by printing JSON events
to stdout; the parent's UIController reads and dispatches them.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import subprocess
import sys
import threading
import tkinter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import customtkinter as ctk
import requests
from PIL import Image

from . import __version__, config, pairing, update
from .autostart import is_autostart_enabled, set_autostart
from .syncthing import SyncthingClient

log = logging.getLogger(__name__)

_WINDOWS = ("pairing", "devices", "settings", "logs", "incoming", "tabbed", "history", "file_picker")


# ---------------------------------------------------------------------------
# Theme helpers
# ---------------------------------------------------------------------------


class Theme:
    """Resolved light/dark color values for the pro theme."""

    def __init__(self, mode: str) -> None:
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        is_dark = mode == "Dark"
        self.bg = config.COLOR_BG_DARK if is_dark else config.COLOR_BG_LIGHT
        self.card = config.COLOR_CARD_DARK if is_dark else config.COLOR_CARD_LIGHT
        self.text = config.COLOR_TEXT_DARK if is_dark else config.COLOR_TEXT_LIGHT
        self.muted = config.COLOR_TEXT_MUTED_DARK if is_dark else config.COLOR_TEXT_MUTED_LIGHT
        self.border = config.COLOR_BORDER_DARK if is_dark else config.COLOR_BORDER_LIGHT
        self.row_bg = config.COLOR_ROW_BG_DARK if is_dark else config.COLOR_ROW_BG_LIGHT

    def card_fg(self) -> tuple[str, str] | str:
        # CustomTkinter tuple format: (light, dark)
        return (config.COLOR_CARD_LIGHT, config.COLOR_CARD_DARK)

    def transparent(self) -> str:
        return "transparent"


# Global resolved theme; set by _run_child() before any windows are built.
THEME = Theme("System")


def _tk_image(img: Image.Image) -> tkinter.PhotoImage:
    """Build a Tk image from a PIL image without going through PIL.ImageTk.

    ImageTk needs _imagingtk to resolve Tcl/Tk symbols at runtime, which fails
    on interpreters that compile _tkinter directly into the binary (the
    python-build-standalone builds uv installs). The failure is a bare
    "bad argument type for built-in operation" that kills the whole window
    process. Tk 8.6 decodes PNG natively, so round-trip through PNG bytes and
    skip Pillow's Tk bridge entirely.

    CTkLabel.configure(image=...) accepts a plain Tk image; it only loses
    CTkImage's HiDPI rescaling, and every caller here already renders at a
    fixed pixel size.
    """
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tkinter.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))


def _center_window(window: ctk.CTkToplevel | ctk.CTk, width: int, height: int) -> None:
    window.update_idletasks()
    sw = window.winfo_screenwidth()
    sh = window.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _fonts() -> dict[str, ctk.CTkFont]:
    """Typography scale used across all windows."""
    return {
        "headline": ctk.CTkFont(size=22, weight="bold"),
        "title": ctk.CTkFont(size=16, weight="bold"),
        "subtitle": ctk.CTkFont(size=12, weight="bold"),
        "body": ctk.CTkFont(size=12),
        "small": ctk.CTkFont(size=11),
        "tiny": ctk.CTkFont(size=10),
        "mono": ctk.CTkFont(family="Menlo", size=11),
    }


def _section_header(parent: ctk.CTkBaseClass, text: str) -> ctk.CTkLabel:
    """Bold subsection title with consistent top spacing."""
    return ctk.CTkLabel(
        parent,
        text=text,
        font=_fonts()["subtitle"],
        text_color=THEME.text,
        anchor="w",
    )


def _card_frame(parent: ctk.CTkBaseClass, **kwargs: Any) -> ctk.CTkFrame:
    """Rounded card container with the current theme background."""
    defaults: dict[str, Any] = dict(
        master=parent,
        fg_color=THEME.card_fg(),
        border_width=1,
        border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
        corner_radius=12,
    )
    defaults.update(kwargs)
    return ctk.CTkFrame(**defaults)


def _primary_button(parent: ctk.CTkBaseClass, text: str, command: Callable[[], None], **kwargs: Any) -> ctk.CTkButton:
    """Main call-to-action button (filled indigo)."""
    defaults: dict[str, Any] = dict(
        master=parent,
        text=text,
        command=command,
        fg_color=config.COLOR_PRIMARY,
        hover_color=config.COLOR_PRIMARY_HOVER,
        text_color="white",
        height=32,
        corner_radius=8,
        font=_fonts()["body"],
    )
    defaults.update(kwargs)
    return ctk.CTkButton(**defaults)


def _secondary_button(parent: ctk.CTkBaseClass, text: str, command: Callable[[], None], **kwargs: Any) -> ctk.CTkButton:
    """Secondary outline button on the card background."""
    defaults: dict[str, Any] = dict(
        master=parent,
        text=text,
        command=command,
        fg_color="transparent",
        hover_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
        text_color=THEME.text,
        border_width=1,
        border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
        height=32,
        corner_radius=8,
        font=_fonts()["body"],
    )
    defaults.update(kwargs)
    return ctk.CTkButton(**defaults)


def _ghost_button(parent: ctk.CTkBaseClass, text: str, command: Callable[[], None], **kwargs: Any) -> ctk.CTkButton:
    """Low-emphasis text-like button colored with the primary accent."""
    defaults: dict[str, Any] = dict(
        master=parent,
        text=text,
        command=command,
        fg_color="transparent",
        hover_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
        text_color=config.COLOR_PRIMARY,
        border_width=0,
        height=28,
        corner_radius=8,
        font=_fonts()["small"],
    )
    defaults.update(kwargs)
    return ctk.CTkButton(**defaults)


def _entry(parent: ctk.CTkBaseClass, **kwargs: Any) -> ctk.CTkEntry:
    """Standard input with themed border and rounded corners."""
    defaults: dict[str, Any] = dict(
        master=parent,
        border_width=1,
        border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
        corner_radius=8,
        font=_fonts()["body"],
    )
    defaults.update(kwargs)
    return ctk.CTkEntry(**defaults)


def _switch(parent: ctk.CTkBaseClass, text: str, variable: ctk.Variable, command: Callable[[], None]) -> ctk.CTkSwitch:
    return ctk.CTkSwitch(
        parent,
        text=text,
        variable=variable,
        command=command,
        progress_color=config.COLOR_PRIMARY,
        button_color=config.COLOR_PRIMARY,
        button_hover_color=config.COLOR_PRIMARY_HOVER,
        text_color=THEME.text,
        font=_fonts()["body"],
    )


# ---------------------------------------------------------------------------
# Parent-side controller
# ---------------------------------------------------------------------------


class UIController:
    """Spawns window subprocesses and forwards their events.

    Safe to call `open()` from any thread (pystray menu callbacks run
    on the tray thread)."""

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def open(self, window: str) -> None:
        # "tabbed:pair" → key "tabbed"; all other names are their own key.
        key = window.split(":")[0]
        if key not in _WINDOWS:
            log.warning("Unknown window: %s", window)
            return
        with self._lock:
            existing = self._procs.get(key)
            if existing is not None and existing.poll() is None:
                return
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "ui", window]
            else:
                cmd = [sys.executable, "-m", "clipsync.ui", window]
            # Route child stderr to the log file so crashes are visible instead
            # of silently swallowed.  Open in binary-append so it doesn't race
            # with the parent's RotatingFileHandler.
            try:
                # Deliberately not a context manager: the handle must outlive
                # this function for the subprocess's stderr; it's closed
                # explicitly below once Popen has its own reference.
                stderr_fh = open(config.LOG_FILE, "ab")  # noqa: SIM115
            except OSError:
                stderr_fh = subprocess.DEVNULL  # type: ignore[assignment]
            kwargs: dict = dict(
                stdout=subprocess.PIPE,
                stderr=stderr_fh,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **kwargs)
            if stderr_fh is not subprocess.DEVNULL:
                stderr_fh.close()
            if sys.platform == "win32":
                # Grant the child permission to bring its window to the
                # foreground.  Without this, Windows' foreground-activation
                # lock silently ignores lift()/focus_force() calls from a
                # process that wasn't directly activated by user input.
                try:
                    import ctypes

                    ctypes.windll.user32.AllowSetForegroundWindow(proc.pid)  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._procs[key] = proc
        threading.Thread(
            target=self._read_events,
            args=(proc,),
            name=f"ui-{key}-reader",
            daemon=True,
        ).start()

    def _read_events(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout is None:
                raise RuntimeError("UI subprocess has no stdout pipe")
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Non-JSON from child: %s", line)
                    continue
                try:
                    self._on_event(evt)
                except Exception:
                    log.exception("UI event handler raised")
        finally:
            proc.wait()
            if proc.returncode not in (0, None, -15):  # -15 = SIGTERM on Unix
                log.warning("UI child process exited with code %d (check log file for details)", proc.returncode)

    def close_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Child-side event emitter
# ---------------------------------------------------------------------------


def _emit(event: str, **payload: object) -> None:
    """Print a JSON event to stdout for the parent to consume."""
    try:
        sys.stdout.write(json.dumps({"event": event, **payload}) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class AppContext:
    """Bag of things a window needs: settings, REST client, device id.

    Mutating callbacks write through settings / REST API directly and
    also emit an event so the parent process can react (restart the
    clipboard watcher, show a tray notification, etc.)."""

    def __init__(
        self,
        settings: config.Settings,
        client: SyncthingClient,
        device_id: str,
    ) -> None:
        self.settings = settings
        self.client = client
        self.device_id = device_id

    def on_pause_changed(self, paused: bool) -> None:
        _emit("pause_changed", paused=paused)

    def on_reset(self) -> None:
        try:
            for d in self.client.connected_devices():
                self.client.remove_device(d["deviceID"])
        except Exception:
            log.exception("Failed to remove devices")
        _emit("reset")

    def on_folder_changed(self, new_path: str) -> None:
        _emit("folder_changed", path=new_path)

    def on_settings_changed(self) -> None:
        _emit("settings_changed")

    def on_accept_device(self, device_id: str) -> None:
        _emit("accept_device", device_id=device_id)

    def on_reject_device(self, device_id: str) -> None:
        _emit("reject_device", device_id=device_id)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class _BaseWindow:
    """Shared Toplevel behavior: fixed size, centered, Escape to close."""

    def __init__(
        self,
        parent: ctk.CTk,
        title: str,
        size: tuple[int, int],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_close = on_close
        self.window = ctk.CTkToplevel(parent)
        self.window.title(title)
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        _center_window(self.window, *size)
        self.window.configure(fg_color=THEME.bg)
        self.window.lift()
        self.window.focus_force()
        self.window.bind("<Escape>", lambda _e: self.close())

    def close(self) -> None:
        try:
            if self.window.winfo_exists():
                self.window.destroy()
        except Exception:
            pass
        if self._on_close is not None:
            cb = self._on_close
            self._on_close = None
            try:
                cb()
            except Exception:
                log.exception("on_close raised")

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except Exception:
            return False

    def focus(self) -> None:
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tab content helpers (usable both standalone and inside TabbedWindow)
# ---------------------------------------------------------------------------


class _PairingContent:
    """Pairing UI: nearby discovery, manual entry, QR display, webcam scan."""

    def __init__(
        self,
        window: ctk.CTk | ctk.CTkToplevel,
        container: ctk.CTkBaseClass,
        app: AppContext,
    ) -> None:
        self._win = window
        self._app = app
        self._scanner: pairing.WebcamQRScanner | None = None
        self._status_var = ctk.StringVar(value="Scan this QR on your other device, or paste its ID below.")
        self._preview_label: ctk.CTkLabel | None = None
        self._preview_size = (300, 225)
        self._scan_container = container

        ctk.CTkLabel(container, text="Pair a device", font=_fonts()["headline"], text_color=THEME.text).pack(
            pady=(0, 18)
        )

        _section_header(container, "Nearby devices").pack(fill="x")
        self._nearby_frame = ctk.CTkScrollableFrame(
            container,
            fg_color=THEME.card_fg(),
            border_width=1,
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            corner_radius=12,
            height=100,
        )
        self._nearby_frame.pack(fill="x", pady=(6, 14))
        self._nearby_seen: set[str] = set()
        self._render_nearby([])
        self._schedule_nearby_refresh()

        _section_header(container, "Or paste a device ID").pack(fill="x", pady=(0, 6))
        entry_row = _card_frame(container)
        entry_row.pack(fill="x", pady=(0, 14))
        self._entry = _entry(entry_row, placeholder_text="XXXXXXX-XXXXXXX-…")
        self._entry.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=10)
        self._entry.bind("<Return>", lambda _e: self._on_add_clicked())
        _ghost_button(entry_row, text="Paste", command=self._on_paste_clicked, width=60).pack(side="left", padx=(0, 6))
        self._add_btn = _primary_button(entry_row, text="Add", command=self._on_add_clicked, width=60)
        self._add_btn.pack(side="left", padx=(0, 12))

        _section_header(container, "Your device ID").pack(fill="x", pady=(0, 6))
        own_row = _card_frame(container)
        own_row.pack(fill="x", pady=(0, 14))
        self._qr_label = ctk.CTkLabel(own_row, text="")
        self._qr_label.pack(side="left", padx=14, pady=14)
        self._render_qr(app.device_id)
        own_right = ctk.CTkFrame(own_row, fg_color="transparent")
        own_right.pack(side="left", fill="both", expand=True, padx=(0, 14), pady=14)
        ctk.CTkLabel(
            own_right,
            text=app.device_id,
            font=_fonts()["small"],
            wraplength=180,
            justify="left",
            anchor="w",
            text_color=THEME.text,
        ).pack(fill="x")
        _primary_button(
            own_right,
            text="Copy ID",
            command=lambda: self._copy_to_clipboard(app.device_id),
        ).pack(fill="x", pady=(10, 0))

        _secondary_button(
            container,
            text="Scan QR with webcam",
            command=self._on_scan_clicked,
        ).pack(fill="x")

        ctk.CTkLabel(
            container,
            textvariable=self._status_var,
            wraplength=360,
            justify="center",
            font=_fonts()["small"],
            text_color=THEME.muted,
        ).pack(pady=(14, 0))

    def _exists(self) -> bool:
        try:
            return bool(self._win.winfo_exists())
        except Exception:
            return False

    def _render_qr(self, device_id: str) -> None:
        qr_img = pairing.generate_qr(device_id, box_size=4, border=2)
        qr_img = qr_img.resize((110, 110), Image.Resampling.NEAREST)
        tk_img = _tk_image(qr_img)
        self._qr_label.configure(image=tk_img)
        self._qr_label.image = tk_img  # keep reference

    def _copy_to_clipboard(self, value: str) -> None:
        try:
            self._win.clipboard_clear()
            self._win.clipboard_append(value)
            self._status_var.set("Device ID copied to clipboard.")
        except Exception:
            log.exception("Clipboard copy failed")

    def _on_paste_clicked(self) -> None:
        try:
            text = self._win.clipboard_get()
        except Exception:
            self._status_var.set("Clipboard is empty.")
            return
        normalized = pairing.normalize_device_id(text)
        self._entry.delete(0, "end")
        if normalized:
            self._entry.insert(0, normalized)
            self._status_var.set("Pasted. Click Add to pair.")
        else:
            self._entry.insert(0, text.strip())
            self._status_var.set("That doesn't look like a valid device ID.")

    def _schedule_nearby_refresh(self) -> None:
        if not self._exists():
            return
        threading.Thread(target=self._nearby_worker, daemon=True).start()
        self._win.after(5000, self._schedule_nearby_refresh)

    def _nearby_worker(self) -> None:
        try:
            discovered = self._app.client.get_discovered_devices()
            known = {d["deviceID"] for d in self._app.client.connected_devices()}
        except Exception:
            return
        if not self._exists():
            return
        items = [did for did in discovered if did != self._app.device_id and did not in known]
        self._win.after(0, lambda: self._render_nearby(items))

    def _render_nearby(self, device_ids: list[str]) -> None:
        if not self._exists():
            return
        for child in self._nearby_frame.winfo_children():
            child.destroy()
        if not device_ids:
            empty = ctk.CTkFrame(self._nearby_frame, fg_color="transparent")
            empty.pack(pady=20)
            ctk.CTkLabel(
                empty,
                text="No nearby devices found",
                font=_fonts()["body"],
                text_color=THEME.muted,
            ).pack()
            ctk.CTkLabel(
                empty,
                text="Make sure the other device is running ClipSync on the same network.",
                font=_fonts()["small"],
                wraplength=300,
                justify="center",
                text_color=THEME.muted,
            ).pack(pady=(4, 0))
            return
        for did in device_ids:
            row = ctk.CTkFrame(self._nearby_frame, fg_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK), corner_radius=8)
            row.pack(fill="x", padx=8, pady=4)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=did[:24] + "…", font=_fonts()["small"], anchor="w", text_color=THEME.text).grid(
                row=0, column=0, sticky="we", padx=12, pady=10
            )
            def _pair_handler(d: str = did) -> None:
                self._pair_from_nearby(d)

            _primary_button(
                row,
                text="Pair",
                width=60,
                command=_pair_handler,
            ).grid(row=0, column=1, padx=(0, 10), pady=6)

    def _pair_from_nearby(self, device_id: str) -> None:
        self._set_pending(device_id)
        threading.Thread(target=self._pair_worker, args=(device_id,), daemon=True).start()

    def _on_add_clicked(self) -> None:
        raw = self._entry.get()
        normalized = pairing.normalize_device_id(raw)
        if not normalized:
            self._status_var.set("That does not look like a valid device ID.")
            return
        self._entry.delete(0, "end")
        self._set_pending(normalized)
        threading.Thread(target=self._pair_worker, args=(normalized,), daemon=True).start()

    def _pair_worker(self, device_id: str) -> None:
        def _set_add_enabled(enabled: bool) -> None:
            try:
                self._add_btn.configure(state="normal" if enabled else "disabled")
            except Exception:
                pass

        self._win.after(0, _set_add_enabled, False)
        try:
            pairing.pair_with_device(self._app.client, device_id)
            self._win.after(0, lambda: self._status_var.set(f"Waiting for {device_id[:7]} to accept…"))
            self._win.after(0, self._start_pending_watch, device_id)
        except Exception as exc:
            log.exception("Pairing failed")
            message = f"Failed to pair: {exc}"
            self._win.after(0, lambda: self._status_var.set(message))
        finally:
            self._win.after(0, _set_add_enabled, True)

    def _set_pending(self, device_id: str) -> None:
        self._status_var.set(f"Adding {device_id[:7]}…")

    def _start_pending_watch(self, device_id: str) -> None:
        def check() -> None:
            try:
                devices = self._app.client.connected_devices()
            except Exception:
                self._win.after(2000, check)
                return
            match = next((d for d in devices if d["deviceID"] == device_id), None)
            if match and match["connected"]:
                self._status_var.set(f"Connected to {device_id[:7]}! Clipboard will sync now.")
                return
            if not self._exists():
                return
            self._win.after(2000, check)

        self._win.after(2000, check)

    def _on_scan_clicked(self) -> None:
        if self._scanner is not None:
            return
        self._status_var.set("Opening camera… point it at the other device's QR.")
        if self._preview_label is None:
            self._preview_label = ctk.CTkLabel(
                self._scan_container,
                text="Starting camera…",
                width=self._preview_size[0],
                height=self._preview_size[1],
            )
            self._preview_label.pack(pady=(8, 10))
        scanner = pairing.WebcamQRScanner(on_detected=self._on_qr_detected)
        scanner.set_frame_callback(self._on_frame)
        self._scanner = scanner
        scanner.start()

    def _on_frame(self, frame: object) -> None:
        try:
            import cv2
        except ImportError:
            return
        try:
            # frame is `object` at this boundary (the callback interface
            # doesn't commit to a cv2/numpy type); narrow it here since this
            # is the one place that actually knows it came from cv2.VideoCapture.
            rgb = cv2.cvtColor(cast(Any, frame), cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            target_w, target_h = self._preview_size
            scale = min(target_w / w, target_h / h)
            nw, nh = int(w * scale), int(h * scale)
            resized = cv2.resize(rgb, (nw, nh))
            img = Image.fromarray(resized)
        except Exception:
            log.exception("Frame conversion failed")
            return

        def update() -> None:
            if self._preview_label is None or not self._exists():
                return
            tk_img = _tk_image(img)
            self._preview_label.configure(image=tk_img, text="")
            self._preview_label.image = tk_img  # keep reference

        try:
            self._win.after(0, update)
        except Exception:
            pass

    def _on_qr_detected(self, device_id: str) -> None:
        def apply() -> None:
            self._status_var.set(f"Scanned {device_id[:7]}, pairing…")
            if self._scanner is not None:
                self._scanner.stop()
                self._scanner = None
            self._set_pending(device_id)
            threading.Thread(target=self._pair_worker, args=(device_id,), daemon=True).start()

        self._win.after(0, apply)

    def stop_scanner(self) -> None:
        if self._scanner is not None:
            try:
                self._scanner.stop()
            except Exception:
                pass
            self._scanner = None


class _DevicesContent:
    """Devices list UI: live status, rename, remove."""

    def __init__(
        self,
        window: ctk.CTk | ctk.CTkToplevel,
        container: ctk.CTkBaseClass,
        app: AppContext,
    ) -> None:
        self._win = window
        self._app = app
        self._refreshing = False

        ctk.CTkLabel(container, text="Connected devices", font=_fonts()["headline"], text_color=THEME.text).pack(
            pady=(0, 18)
        )

        self._list_frame = ctk.CTkScrollableFrame(
            container,
            fg_color=THEME.card_fg(),
            border_width=1,
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            corner_radius=12,
        )
        self._list_frame.pack(fill="both", expand=True)

        _primary_button(container, text="Refresh", command=self._refresh).pack(fill="x", pady=(16, 0))

        self._refresh()
        self._schedule_refresh()

    def _exists(self) -> bool:
        try:
            return bool(self._win.winfo_exists())
        except Exception:
            return False

    def _schedule_refresh(self) -> None:
        if not self._exists():
            return
        self._win.after(10_000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if not self._exists():
            return
        self._refresh()
        self._schedule_refresh()

    def _refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        for child in self._list_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(self._list_frame, text="Loading…", text_color=("gray50", "gray60")).pack(pady=20)
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self) -> None:
        try:
            devices: list[dict] = self._app.client.connected_devices()
            error: str | None = None
        except requests.RequestException:
            devices = []
            error = "Syncthing is not responding"
        except Exception as exc:
            devices = []
            error = str(exc)
        if self._exists():
            self._win.after(0, self._apply_refresh, devices, error)
        else:
            self._refreshing = False

    def _apply_refresh(self, devices: list[dict], error: str | None) -> None:
        self._refreshing = False
        for child in self._list_frame.winfo_children():
            child.destroy()
        if error:
            ctk.CTkLabel(self._list_frame, text=error, text_color=config.COLOR_DANGER, font=_fonts()["body"]).pack(pady=14)
            return
        if not devices:
            empty = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            empty.pack(pady=40)
            ctk.CTkLabel(
                empty,
                text="No devices paired yet.",
                font=_fonts()["title"],
                text_color=THEME.muted,
            ).pack()
            ctk.CTkLabel(
                empty,
                text="Go to the Pair tab to connect a device.",
                font=_fonts()["small"],
                text_color=THEME.muted,
            ).pack(pady=(4, 0))
            return
        for d in devices:
            self._build_row(d)

    def _build_row(self, device: dict) -> None:
        row = ctk.CTkFrame(self._list_frame, fg_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK), corner_radius=10)
        row.pack(fill="x", padx=8, pady=5)
        row.grid_columnconfigure(0, weight=1)

        name_text = device.get("name") or device["deviceID"][:7]
        ctk.CTkLabel(row, text=name_text, font=_fonts()["subtitle"], text_color=THEME.text, anchor="w").grid(
            row=0, column=0, sticky="we", padx=12, pady=(10, 0)
        )
        ctk.CTkLabel(row, text=device["deviceID"][:24] + "…", font=_fonts()["tiny"], text_color=THEME.muted, anchor="w").grid(
            row=1, column=0, sticky="we", padx=12, pady=(0, 10)
        )

        status_color = config.COLOR_SUCCESS if device["connected"] else THEME.muted
        status_text = "● Connected" if device["connected"] else "○ Offline"
        ctk.CTkLabel(row, text=status_text, text_color=status_color, font=_fonts()["small"]).grid(
            row=0, column=1, rowspan=2, padx=12
        )

        def _rename_handler(
            did: str = device["deviceID"],
            nm: str = name_text,
        ) -> None:
            self._rename_device(did, nm)

        def _remove_handler(did: str = device["deviceID"]) -> None:
            self._remove_device(did)

        _ghost_button(
            row,
            text="Rename",
            width=70,
            command=_rename_handler,
        ).grid(row=0, column=2, rowspan=2, padx=(0, 4))

        _secondary_button(
            row,
            text="Remove",
            width=70,
            command=_remove_handler,
        ).grid(row=0, column=3, rowspan=2, padx=(0, 12))

    def _remove_device(self, device_id: str) -> None:
        try:
            self._app.client.remove_device(device_id)
        except Exception:
            log.exception("Failed to remove device")
        self._refresh()

    def _rename_device(self, device_id: str, current_name: str) -> None:
        dialog = ctk.CTkToplevel(self._win)
        dialog.title("Rename device")
        dialog.resizable(False, False)
        _center_window(dialog, 320, 160)
        dialog.transient(self._win)
        dialog.grab_set()

        dialog.configure(fg_color=THEME.bg)
        container = ctk.CTkFrame(dialog, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            container,
            text="Rename device",
            font=_fonts()["title"],
            text_color=THEME.text,
        ).pack(anchor="w")
        ctk.CTkLabel(
            container,
            text=f"Choose a new name for {device_id[:7]}.",
            font=_fonts()["small"],
            text_color=THEME.muted,
        ).pack(anchor="w", pady=(4, 12))

        entry = _entry(container)
        entry.insert(0, current_name)
        entry.pack(fill="x")
        entry.select_range(0, "end")
        entry.focus_set()

        btns = ctk.CTkFrame(container, fg_color="transparent")
        btns.pack(fill="x", pady=(16, 0))

        def do_save() -> None:
            new_name = entry.get().strip()
            if not new_name:
                return
            try:
                self._app.client.rename_device(device_id, new_name)
            except Exception:
                log.exception("Rename failed")
            dialog.destroy()
            self._refresh()

        _secondary_button(btns, text="Cancel", command=dialog.destroy).pack(
            side="left", expand=True, fill="x", padx=(0, 6)
        )
        _primary_button(btns, text="Save", command=do_save).pack(side="left", expand=True, fill="x", padx=(6, 0))
        entry.bind("<Return>", lambda _e: do_save())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())


class _SettingsContent:
    """Settings UI: toggles, passphrase, folder, logs, reset, update check."""

    def __init__(
        self,
        window: ctk.CTk | ctk.CTkToplevel,
        container: ctk.CTkBaseClass,
        app: AppContext,
    ) -> None:
        self._win = window
        self._app = app
        self._logs_window: LogsWindow | None = None

        ctk.CTkLabel(container, text="Settings", font=_fonts()["headline"], text_color=THEME.text).pack(
            anchor="w", pady=(0, 18)
        )

        general_card = _card_frame(container)
        general_card.pack(fill="x", pady=(0, 16))
        _section_header(general_card, "General").pack(anchor="w", padx=16, pady=(14, 8))

        self._autostart_var = ctk.BooleanVar(value=is_autostart_enabled())
        _switch(general_card, "Start on login", self._autostart_var, self._on_autostart_toggle).pack(
            anchor="w", padx=16, pady=4
        )

        self._notify_var = ctk.BooleanVar(value=bool(app.settings.get("show_notifications")))
        _switch(general_card, "Show notifications on sync", self._notify_var, self._on_notify_toggle).pack(
            anchor="w", padx=16, pady=4
        )

        self._sync_enabled_var = ctk.BooleanVar(value=not bool(app.settings.get("sync_paused")))
        _switch(general_card, "Sync enabled", self._sync_enabled_var, self._on_sync_enabled_toggle).pack(
            anchor="w", padx=16, pady=4
        )

        self._auto_accept_var = ctk.BooleanVar(value=bool(app.settings.get("auto_accept_incoming")))
        _switch(
            general_card,
            "Auto-accept incoming requests (no prompt)",
            self._auto_accept_var,
            self._on_auto_accept_toggle,
        ).pack(anchor="w", padx=16, pady=(4, 14))

        privacy_card = _card_frame(container)
        privacy_card.pack(fill="x", pady=(0, 16))
        _section_header(privacy_card, "Privacy & security").pack(anchor="w", padx=16, pady=(14, 8))

        self._log_mirror_var = ctk.BooleanVar(value=bool(app.settings.get("debug_log_mirror")))
        _switch(
            privacy_card,
            "Share my log with paired devices (for debugging)",
            self._log_mirror_var,
            self._on_log_mirror_toggle,
        ).pack(anchor="w", padx=16, pady=(4, 2))
        ctk.CTkLabel(
            privacy_card,
            text="Copies this device's log into the synced folder. No clipboard text is logged.",
            font=_fonts()["tiny"],
            justify="left",
            text_color=THEME.muted,
        ).pack(anchor="w", padx=(52, 16), pady=(0, 14))

        _section_header(privacy_card, "Encryption passphrase (optional)").pack(
            anchor="w", padx=16, pady=(4, 2)
        )
        ctk.CTkLabel(
            privacy_card,
            text="Same passphrase on every device. Empty = no encryption.",
            font=_fonts()["tiny"],
            text_color=THEME.muted,
        ).pack(anchor="w", padx=16)
        passphrase_row = ctk.CTkFrame(privacy_card, fg_color="transparent")
        passphrase_row.pack(fill="x", padx=16, pady=(6, 16))
        self._passphrase_entry = _entry(passphrase_row, show="•")
        self._passphrase_entry.insert(0, str(app.settings.get("encryption_passphrase") or ""))
        self._passphrase_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _primary_button(passphrase_row, text="Save", command=self._on_save_passphrase, width=70).pack(side="left")

        appearance_card = _card_frame(container)
        appearance_card.pack(fill="x", pady=(0, 16))
        _section_header(appearance_card, "Appearance").pack(anchor="w", padx=16, pady=(14, 8))
        theme_row = ctk.CTkFrame(appearance_card, fg_color="transparent")
        theme_row.pack(fill="x", padx=16, pady=(0, 14))
        self._theme_seg = ctk.CTkSegmentedButton(
            theme_row,
            values=["Light", "Dark", "System"],
            command=self._on_theme_changed,
            fg_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            selected_color=config.COLOR_PRIMARY,
            selected_hover_color=config.COLOR_PRIMARY_HOVER,
            unselected_color=(config.COLOR_CARD_LIGHT, config.COLOR_CARD_DARK),
            unselected_hover_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
            text_color=THEME.text,
            text_color_disabled=THEME.muted,
        )
        self._theme_seg.pack(side="left")
        current_theme = str(app.settings.get("theme") or "System")
        if current_theme in ("Light", "Dark", "System"):
            self._theme_seg.set(current_theme)
        else:
            self._theme_seg.set("System")

        history_card = _card_frame(container)
        history_card.pack(fill="x", pady=(0, 16))
        _section_header(history_card, "Clipboard history auto-clear").pack(anchor="w", padx=16, pady=(14, 8))
        self._auto_clear_options: dict[str, int] = {
            "Never": 0,
            "5 minutes": 5,
            "15 minutes": 15,
            "30 minutes": 30,
            "1 hour": 60,
            "4 hours": 240,
            "24 hours": 1440,
        }
        auto_clear_row = ctk.CTkFrame(history_card, fg_color="transparent")
        auto_clear_row.pack(fill="x", padx=16, pady=(0, 14))
        current_auto_clear = int(app.settings.get("history_auto_clear_minutes") or 0)
        auto_clear_label = {v: k for k, v in self._auto_clear_options.items()}.get(current_auto_clear, "Never")
        self._auto_clear_menu = ctk.CTkOptionMenu(
            auto_clear_row,
            values=list(self._auto_clear_options.keys()),
            command=self._on_auto_clear_changed,
            fg_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
            button_color=config.COLOR_PRIMARY,
            button_hover_color=config.COLOR_PRIMARY_HOVER,
            text_color=THEME.text,
            dropdown_fg_color=(config.COLOR_CARD_LIGHT, config.COLOR_CARD_DARK),
            dropdown_hover_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
            dropdown_text_color=THEME.text,
            corner_radius=8,
        )
        self._auto_clear_menu.pack(side="left")
        self._auto_clear_menu.set(auto_clear_label)

        advanced_card = _card_frame(container)
        advanced_card.pack(fill="x", pady=(0, 16))
        _section_header(advanced_card, "Advanced").pack(anchor="w", padx=16, pady=(14, 8))

        _section_header(advanced_card, "Sync folder path").pack(anchor="w", padx=16)
        ctk.CTkLabel(
            advanced_card,
            text="Changing this requires a restart to take effect.",
            font=_fonts()["tiny"],
            text_color=THEME.muted,
        ).pack(anchor="w", padx=16)
        folder_row = ctk.CTkFrame(advanced_card, fg_color="transparent")
        folder_row.pack(fill="x", padx=16, pady=(6, 14))
        self._folder_entry = _entry(folder_row)
        self._folder_entry.insert(0, str(app.settings.get("sync_folder") or config.SYNC_FOLDER))
        self._folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _primary_button(folder_row, text="Save", command=self._on_save_folder, width=70).pack(side="left")

        _secondary_button(
            advanced_card,
            text="View Syncthing logs",
            command=self._on_view_logs,
        ).pack(fill="x", padx=16, pady=(0, 14))

        danger_card = _card_frame(container)
        danger_card.pack(fill="x", pady=(0, 16))
        _section_header(danger_card, "Danger zone").pack(anchor="w", padx=16, pady=(14, 8))
        _primary_button(
            danger_card,
            text="Reset / unpair all devices",
            fg_color=config.COLOR_DANGER,
            hover_color=config.COLOR_DANGER_HOVER,
            command=self._on_reset,
        ).pack(fill="x", padx=16, pady=(0, 14))

        update_card = _card_frame(container)
        update_card.pack(fill="x", pady=(0, 16))
        _section_header(update_card, "Updates").pack(anchor="w", padx=16, pady=(14, 8))
        update_row = ctk.CTkFrame(update_card, fg_color="transparent")
        update_row.pack(fill="x", padx=16, pady=(0, 14))
        self._update_btn = _secondary_button(
            update_row,
            text=f"Check for updates (v{__version__})",
            command=self._on_check_update,
        )
        self._update_btn.pack(fill="x")
        self._download_btn: ctk.CTkButton | None = None
        self._update_url = update.RELEASES_HTML_URL

        self._status = ctk.CTkLabel(container, text="", font=_fonts()["small"], text_color=THEME.muted)
        self._status.pack(pady=(8, 0))

    def _exists(self) -> bool:
        try:
            return bool(self._win.winfo_exists())
        except Exception:
            return False

    def _on_autostart_toggle(self) -> None:
        enabled = bool(self._autostart_var.get())
        set_autostart(enabled)
        self._app.settings.set("start_on_login", enabled)
        self._status.configure(text=f"Start on login {'enabled' if enabled else 'disabled'}.")

    def _on_notify_toggle(self) -> None:
        self._app.settings.set("show_notifications", bool(self._notify_var.get()))

    def _on_sync_enabled_toggle(self) -> None:
        enabled = bool(self._sync_enabled_var.get())
        paused = not enabled
        self._app.settings.set("sync_paused", paused)
        self._app.on_pause_changed(paused)
        self._status.configure(text=f"Sync {'enabled' if enabled else 'paused'}.")

    def _on_log_mirror_toggle(self) -> None:
        enabled = bool(self._log_mirror_var.get())
        self._app.settings.set("debug_log_mirror", enabled)
        self._app.on_settings_changed()
        self._status.configure(
            text=(
                "Log sharing on. Paired devices will receive this device's log."
                if enabled
                else "Log sharing off. Your published log will be removed shortly."
            )
        )

    def _on_auto_accept_toggle(self) -> None:
        enabled = bool(self._auto_accept_var.get())
        self._app.settings.set("auto_accept_incoming", enabled)
        self._app.on_settings_changed()
        self._status.configure(
            text=(
                "Auto-accept enabled. New requests will pair immediately."
                if enabled
                else "Auto-accept disabled. You'll be prompted before pairing."
            )
        )

    def _on_theme_changed(self, value: str) -> None:
        self._app.settings.set("theme", value)
        self._app.on_settings_changed()
        ctk.set_appearance_mode(value)
        self._status.configure(text=f"Theme set to {value}.")

    def _on_auto_clear_changed(self, value: str) -> None:
        minutes = self._auto_clear_options.get(value, 0)
        self._app.settings.set("history_auto_clear_minutes", minutes)
        self._app.on_settings_changed()
        self._status.configure(
            text=(f"History auto-clear set to {value}." if minutes > 0 else "History auto-clear disabled.")
        )

    def _on_save_passphrase(self) -> None:
        new_value = self._passphrase_entry.get()
        self._app.settings.set("encryption_passphrase", new_value)
        self._app.on_settings_changed()
        if new_value:
            self._status.configure(text="Encryption enabled. Set the same passphrase on every device.")
        else:
            self._status.configure(text="Encryption disabled.")

    def _on_save_folder(self) -> None:
        new_path = self._folder_entry.get().strip()
        if not new_path:
            self._status.configure(text="Folder path cannot be empty.")
            return
        try:
            Path(new_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._status.configure(text=f"Cannot use folder: {exc}")
            return
        self._app.settings.set("sync_folder", new_path)
        self._app.on_folder_changed(new_path)
        self._status.configure(text="Folder updated. Restart for Syncthing changes.")

    def _on_view_logs(self) -> None:
        if self._logs_window is not None and self._logs_window.exists():
            self._logs_window.focus()
            return
        self._logs_window = LogsWindow(self._win.master, on_close=self._on_logs_closed)

    def _on_logs_closed(self) -> None:
        self._logs_window = None

    def _on_check_update(self) -> None:
        self._status.configure(text="Checking for updates…")
        self._update_btn.configure(state="disabled")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        try:
            info = update.check_for_update()
        except Exception as exc:
            log.exception("Update check failed")
            message = f"Couldn't check for updates: {exc}"
            self._win.after(0, lambda: self._finish_update_check(None, message))
            return
        self._win.after(0, lambda: self._finish_update_check(info, None))

    def _finish_update_check(self, info: update.UpdateInfo | None, error: str | None) -> None:
        if not self._exists():
            return
        try:
            self._update_btn.configure(state="normal")
        except Exception:
            pass
        if error is not None:
            self._status.configure(text=error)
            return
        if info is None:
            return
        if not info.update_available:
            self._status.configure(text=f"You're up to date (v{info.current_version}).")
            return
        self._update_url = info.release_url
        self._status.configure(text=f"Update available: v{info.latest_version} (you have v{info.current_version}).")
        if self._download_btn is None:
            self._download_btn = _primary_button(
                self._update_btn.master,
                text="Download update",
                command=self._on_download_clicked,
            )
            self._download_btn.pack(fill="x", pady=(10, 0))

    def _on_download_clicked(self) -> None:
        if update.open_download_page(self._update_url):
            self._status.configure(text="Opened the download page in your browser.")
        else:
            self._status.configure(text=f"Couldn't open the browser. Visit: {self._update_url}")

    def _on_reset(self) -> None:
        confirm = ctk.CTkToplevel(self._win)
        confirm.title("Confirm reset")
        confirm.configure(fg_color=THEME.bg)
        confirm.resizable(False, False)
        _center_window(confirm, 340, 170)
        confirm.bind("<Escape>", lambda _e: confirm.destroy())
        container = ctk.CTkFrame(confirm, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=24, pady=24)

        ctk.CTkLabel(
            container,
            text="Reset everything?",
            font=_fonts()["title"],
            text_color=THEME.text,
        ).pack(anchor="w")
        ctk.CTkLabel(
            container,
            text="This removes all paired devices. You will need to re-pair them.",
            font=_fonts()["small"],
            text_color=THEME.muted,
            justify="left",
            wraplength=280,
        ).pack(anchor="w", pady=(6, 18))

        btns = ctk.CTkFrame(container, fg_color="transparent")
        btns.pack(fill="x")

        def do_reset() -> None:
            confirm.destroy()
            self._app.on_reset()
            self._status.configure(text="All devices removed.")

        _secondary_button(btns, text="Cancel", command=confirm.destroy).pack(
            side="left", expand=True, fill="x", padx=(0, 6)
        )
        _primary_button(
            btns,
            text="Reset",
            fg_color=config.COLOR_DANGER,
            hover_color=config.COLOR_DANGER_HOVER,
            command=do_reset,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))


# ---------------------------------------------------------------------------
# Window classes (thin wrappers around content helpers)
# ---------------------------------------------------------------------------


class PairingWindow(_BaseWindow):
    """QR code of our device ID + manual entry + webcam scan."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Add Device", config.PAIRING_WINDOW_SIZE, on_close)
        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=16)
        self._content = _PairingContent(self.window, container, app)

    def close(self) -> None:
        self._content.stop_scanner()
        super().close()


class DevicesWindow(_BaseWindow):
    """List of paired devices with live connection status."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Connected Devices", (420, 420), on_close)
        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)
        _DevicesContent(self.window, container, app)


class SettingsWindow(_BaseWindow):
    """Toggles for autostart, notifications, pause, sync folder, reset."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Settings", (440, 560), on_close)
        container = ctk.CTkScrollableFrame(
            self.window,
            fg_color=THEME.bg,
            scrollbar_button_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
        )
        container.pack(fill="both", expand=True, padx=16, pady=16)
        _SettingsContent(self.window, container, app)


class TabbedWindow(_BaseWindow):
    """Single window with Devices / Pair / Settings tabs."""

    _TAB_NAMES = ("Devices", "Pair", "Settings")
    _TAB_MAP = {"devices": "Devices", "pair": "Pair", "settings": "Settings"}

    def __init__(
        self,
        parent: ctk.CTk,
        app: AppContext,
        on_close: Callable[[], None],
        initial_tab: str = "Devices",
    ) -> None:
        super().__init__(parent, config.APP_NAME, (560, 620), on_close)
        self.window.resizable(True, True)

        tabs = ctk.CTkTabview(
            self.window,
            fg_color=THEME.bg,
            segmented_button_fg_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            segmented_button_selected_color=config.COLOR_PRIMARY,
            segmented_button_selected_hover_color=config.COLOR_PRIMARY_HOVER,
            segmented_button_unselected_color=THEME.card_fg(),
            segmented_button_unselected_hover_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK),
            text_color=THEME.text,
        )
        tabs.pack(fill="both", expand=True, padx=12, pady=12)
        for name in self._TAB_NAMES:
            tabs.add(name)

        dev_frame = ctk.CTkFrame(tabs.tab("Devices"), fg_color="transparent")
        dev_frame.pack(fill="both", expand=True, padx=14, pady=10)
        _DevicesContent(self.window, dev_frame, app)

        pair_frame = ctk.CTkFrame(tabs.tab("Pair"), fg_color="transparent")
        pair_frame.pack(fill="both", expand=True, padx=14, pady=10)
        self._pairing = _PairingContent(self.window, pair_frame, app)

        settings_frame = ctk.CTkScrollableFrame(
            tabs.tab("Settings"),
            fg_color=THEME.bg,
            scrollbar_button_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
        )
        settings_frame.pack(fill="both", expand=True, padx=14, pady=10)
        _SettingsContent(self.window, settings_frame, app)

        if initial_tab in self._TAB_NAMES:
            tabs.set(initial_tab)

    def close(self) -> None:
        self._pairing.stop_scanner()
        super().close()


class LogsWindow(_BaseWindow):
    """Read-only tail of the ClipSync log file."""

    def __init__(self, parent: ctk.CTk, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Logs", (640, 440), on_close)
        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(container, text="Logs", font=_fonts()["headline"], text_color=THEME.text).pack(anchor="w", pady=(0, 12))
        self._textbox = ctk.CTkTextbox(
            container,
            wrap="none",
            font=_fonts()["mono"],
            fg_color=(config.COLOR_CARD_LIGHT, config.COLOR_CARD_DARK),
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            border_width=1,
            corner_radius=10,
        )
        self._textbox.pack(fill="both", expand=True)
        self._refresh()
        _primary_button(container, text="Refresh", command=self._refresh).pack(fill="x", pady=(12, 0))

    def _refresh(self) -> None:
        try:
            text = config.LOG_FILE.read_text(encoding="utf-8") if config.LOG_FILE.exists() else "(no logs yet)"
        except OSError as exc:
            text = f"(could not read log: {exc})"
        tail = "\n".join(text.splitlines()[-400:])
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.insert("1.0", tail)
        self._textbox.configure(state="disabled")
        self._textbox.see("end")


class IncomingWindow(_BaseWindow):
    """List of devices asking to connect, with Accept / Reject buttons.

    Pending requests are read directly from Syncthing via the REST API.
    Each click emits an event so the parent process can perform the
    actual config change (add device + share folder) or record a
    rejection — keeping all Syncthing mutations on the parent side."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Incoming Requests", (440, 360), on_close)
        self._app = app
        self._handled: set[str] = set()
        self._refreshing = False

        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            container, text="Incoming device requests", font=_fonts()["headline"], text_color=THEME.text
        ).pack(pady=(0, 6))
        ctk.CTkLabel(
            container,
            text="Accept a device to start syncing clipboard with it.",
            font=_fonts()["small"],
            text_color=THEME.muted,
        ).pack(pady=(0, 16))

        self._list_frame = ctk.CTkScrollableFrame(
            container,
            fg_color=THEME.card_fg(),
            border_width=1,
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            corner_radius=12,
        )
        self._list_frame.pack(fill="both", expand=True)

        self._status = ctk.CTkLabel(container, text="", font=_fonts()["small"], text_color=THEME.muted)
        self._status.pack(pady=(10, 0))

        self._refresh()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if not self.exists():
            return
        self.window.after(10_000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if not self.exists():
            return
        self._refresh()
        self._schedule_refresh()

    def _refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        for child in self._list_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(self._list_frame, text="Loading…", text_color=("gray50", "gray60")).pack(pady=20)
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self) -> None:
        try:
            pending: dict = self._app.client.get_pending_devices() or {}
            error: str | None = None
        except requests.RequestException:
            pending = {}
            error = "Syncthing is not responding"
        except Exception as exc:
            pending = {}
            error = str(exc)
        if self.exists():
            self.window.after(0, self._apply_refresh, pending, error)
        else:
            self._refreshing = False

    def _apply_refresh(self, pending: dict, error: str | None) -> None:
        self._refreshing = False
        for child in self._list_frame.winfo_children():
            child.destroy()
        if error:
            ctk.CTkLabel(self._list_frame, text=error, text_color=config.COLOR_DANGER, font=_fonts()["body"]).pack(pady=14)
            return
        rejected = set(self._app.settings.get("rejected_device_ids") or [])
        visible = [
            (did, info or {})
            for did, info in pending.items()
            if pairing.normalize_device_id(did) and did not in rejected and did not in self._handled
        ]
        if not visible:
            empty = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            empty.pack(pady=40)
            ctk.CTkLabel(
                empty,
                text="No pending requests.",
                font=_fonts()["title"],
                text_color=THEME.muted,
            ).pack()
            ctk.CTkLabel(
                empty,
                text="Ask the other device to pair with this one.",
                font=_fonts()["small"],
                text_color=THEME.muted,
            ).pack(pady=(4, 0))
            return
        for device_id, info in visible:
            self._build_row(device_id, info)

    def _build_row(self, device_id: str, info: dict) -> None:
        row = ctk.CTkFrame(self._list_frame, fg_color=(config.COLOR_ROW_BG_LIGHT, config.COLOR_ROW_BG_DARK), corner_radius=10)
        row.pack(fill="x", padx=8, pady=5)
        row.grid_columnconfigure(0, weight=1)

        name = info.get("name") or device_id[:7]
        ctk.CTkLabel(row, text=str(name), font=_fonts()["subtitle"], text_color=THEME.text, anchor="w").grid(
            row=0, column=0, sticky="we", padx=12, pady=(10, 0)
        )
        ctk.CTkLabel(row, text=device_id[:24] + "…", font=_fonts()["tiny"], text_color=THEME.muted, anchor="w").grid(
            row=1, column=0, sticky="we", padx=12, pady=(0, 10)
        )

        def _accept_handler(did: str = device_id) -> None:
            self._accept(did)

        def _reject_handler(did: str = device_id) -> None:
            self._reject(did)

        _primary_button(
            row,
            text="Accept",
            width=70,
            command=_accept_handler,
        ).grid(row=0, column=1, rowspan=2, padx=(0, 4))

        _secondary_button(
            row,
            text="Reject",
            width=70,
            command=_reject_handler,
        ).grid(row=0, column=2, rowspan=2, padx=(0, 12))

    def _accept(self, device_id: str) -> None:
        self._handled.add(device_id)
        self._app.on_accept_device(device_id)
        self._status.configure(text=f"Accepted {device_id[:7]}.")
        self._refresh()

    def _reject(self, device_id: str) -> None:
        self._handled.add(device_id)
        self._app.on_reject_device(device_id)
        self._status.configure(text=f"Rejected {device_id[:7]}.")
        self._refresh()


# ---------------------------------------------------------------------------
# Child-process entry point
# ---------------------------------------------------------------------------


class HistoryWindow(_BaseWindow):
    """Scrollable list of recent clipboard entries with copy-on-click and search."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        self._app = app
        super().__init__(parent, f"{config.APP_NAME} — Clipboard History", (480, 520), on_close)
        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=16, pady=16)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Clipboard History", font=_fonts()["headline"], text_color=THEME.text).pack(side="left")
        self._status = ctk.CTkLabel(header, text="", font=_fonts()["small"], text_color=THEME.muted)
        self._status.pack(side="right")

        search_row = _card_frame(container)
        search_row.pack(fill="x", pady=(0, 12))
        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._refresh())
        self._search_entry = _entry(
            search_row,
            placeholder_text="Search history…",
            textvariable=self._search_var,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=10, pady=10)
        self._search_entry.bind("<Escape>", lambda _e: (self._search_var.set(""), self._search_entry.focus_set()))

        self._list_frame = ctk.CTkScrollableFrame(
            container,
            fg_color=THEME.card_fg(),
            border_width=1,
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            corner_radius=12,
        )
        self._list_frame.pack(fill="both", expand=True)

        btn_row = ctk.CTkFrame(container, fg_color="transparent")
        btn_row.pack(fill="x", pady=(14, 0))
        _primary_button(btn_row, text="Refresh", command=self._refresh, width=100).pack(side="left")
        _secondary_button(btn_row, text="Clear All", command=self._confirm_clear, width=100).pack(side="right")

        self._all_entries: list[object] = []
        self._refresh()

    def _refresh(self) -> None:
        from .history import ClipboardHistory

        for w in self._list_frame.winfo_children():
            w.destroy()

        history = ClipboardHistory(self._app.settings)
        self._all_entries = list(reversed(history.get_entries()))

        query = self._search_var.get().strip().lower()
        if query:
            entries = [e for e in self._all_entries if query in getattr(e, "text", "").lower()]
        else:
            entries = self._all_entries

        if not entries:
            empty = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            empty.pack(pady=40)
            if query:
                ctk.CTkLabel(
                    empty,
                    text="No matches found.",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    text_color=("gray30", "gray70"),
                ).pack()
                ctk.CTkLabel(
                    empty,
                    text="Try a different search term.",
                    font=ctk.CTkFont(size=11),
                    text_color=("gray30", "gray70"),
                ).pack(pady=(4, 0))
            else:
                ctk.CTkLabel(
                    empty,
                    text="No clipboard history yet.",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    text_color=("gray30", "gray70"),
                ).pack()
                ctk.CTkLabel(
                    empty,
                    text="Items appear here as you copy text.",
                    font=ctk.CTkFont(size=11),
                    text_color=("gray30", "gray70"),
                ).pack(pady=(4, 0))
            self._status.configure(text=f"0 / {len(self._all_entries)} entries")
            return

        self._status.configure(text=f"{len(entries)} / {len(self._all_entries)} entries")

        import time as _time

        now = _time.time()
        for entry in entries:
            self._build_row(entry, now)

    def _build_row(self, entry: object, now: float) -> None:
        from datetime import datetime

        ts = getattr(entry, "timestamp", 0.0)
        text = getattr(entry, "text", "")
        source = getattr(entry, "source", "local")

        dt = datetime.fromtimestamp(ts)
        age = now - ts
        time_str = dt.strftime("%I:%M %p").lstrip("0") if age < 86400 else dt.strftime("%b %d %I:%M %p").lstrip("0")

        preview = text.replace("\n", " ").replace("\r", " ")
        if len(preview) > 72:
            preview = preview[:72] + "..."

        row = ctk.CTkFrame(
            self._list_frame,
            fg_color=(config.COLOR_CARD_LIGHT, config.COLOR_CARD_DARK),
            border_width=1,
            border_color=(config.COLOR_BORDER_LIGHT, config.COLOR_BORDER_DARK),
            corner_radius=10,
        )
        row.pack(fill="x", padx=6, pady=4)
        row.grid_columnconfigure(1, weight=1)

        source_label = "Remote" if source == "remote" else "Local"
        source_color = config.COLOR_PRIMARY if source == "remote" else THEME.muted
        meta = ctk.CTkLabel(
            row,
            text=f"{time_str}  •  {source_label}",
            font=_fonts()["tiny"],
            text_color=source_color,
            anchor="w",
        )
        meta.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 0))

        preview_label = ctk.CTkLabel(
            row,
            text=preview or "(empty)",
            font=_fonts()["small"],
            text_color=THEME.text,
            anchor="w",
            justify="left",
        )
        preview_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 8))

        def _copy_handler(t: str = text) -> None:
            self._copy_entry(t)

        copy_btn = _primary_button(
            row,
            text="Copy",
            width=56,
            command=_copy_handler,
        )
        copy_btn.grid(row=0, column=2, rowspan=2, padx=(0, 10), pady=6)

    def _copy_entry(self, text: str) -> None:
        try:
            import pyperclip

            pyperclip.copy(text)
            self._status.configure(text="Copied!")
            self.window.after(1500, lambda: self._status.configure(text=""))
        except Exception as exc:
            log.warning("Copy failed: %s", exc)

    def _confirm_clear(self) -> None:
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Clear History")
        dialog.configure(fg_color=THEME.bg)
        dialog.resizable(False, False)
        _center_window(dialog, 340, 170)
        dialog.lift()
        dialog.focus_force()
        dialog.grab_set()
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

        container = ctk.CTkFrame(dialog, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=24, pady=24)
        ctk.CTkLabel(
            container,
            text="Clear all clipboard history?",
            font=_fonts()["title"],
            text_color=THEME.text,
        ).pack(anchor="w")
        ctk.CTkLabel(
            container,
            text="This cannot be undone.",
            font=_fonts()["small"],
            text_color=THEME.muted,
        ).pack(anchor="w", pady=(6, 18))

        btn_row = ctk.CTkFrame(container, fg_color="transparent")
        btn_row.pack(fill="x")

        def do_clear() -> None:
            _emit("clear_history")
            from .history import ClipboardHistory

            ClipboardHistory(self._app.settings).clear()
            dialog.destroy()
            self._refresh()

        _secondary_button(btn_row, text="Cancel", command=dialog.destroy).pack(
            side="left", expand=True, fill="x", padx=(0, 6)
        )
        _primary_button(
            btn_row,
            text="Clear All",
            fg_color=config.COLOR_DANGER,
            hover_color=config.COLOR_DANGER_HOVER,
            command=do_clear,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))


def _run_child(window_name: str) -> int:
    """Entry point invoked as `python -m clipsync.ui <window>` by UIController."""
    config.configure_logging()
    settings = config.Settings()
    api_key = settings.get("api_key")
    if not api_key:
        log.error("No api_key in settings; parent has not initialized Syncthing")
        return 1
    client = SyncthingClient(api_key)
    try:
        device_id = client.get_device_id()
    except Exception:
        log.exception("Could not fetch device id from Syncthing")
        device_id = ""

    app = AppContext(settings=settings, client=client, device_id=device_id)

    # Disable ctk's Windows titlebar manipulation on both CTk and CTkToplevel.
    # Without this, CTk.__init__ triggers a withdraw/update() dance that makes
    # the hidden root window briefly appear, and CTkToplevel.resizable() queues
    # further deiconify callbacks that race with our own withdraw() calls and
    # can leave windows permanently hidden.  We lose the dark titlebar on
    # Windows; that's an acceptable trade for windows that reliably open.
    ctk.CTk._deactivate_windows_window_header_manipulation = True
    ctk.CTkToplevel._deactivate_windows_window_header_manipulation = True
    theme = str(settings.get("theme") or "System")
    if theme not in ("Light", "Dark", "System"):
        theme = "System"
    ctk.set_appearance_mode(theme)
    ctk.set_default_color_theme("dark-blue")
    THEME.set_mode(theme)
    root = ctk.CTk()
    root.withdraw()
    root.configure(fg_color=THEME.bg)

    def _quit() -> None:
        try:
            root.quit()
        except Exception:
            pass

    # "tabbed:pair" → kind="tabbed", tab_hint="pair"
    parts = window_name.split(":", 1)
    kind = parts[0]
    tab_hint = parts[1] if len(parts) > 1 else ""

    if kind == "tabbed":
        initial_tab = TabbedWindow._TAB_MAP.get(tab_hint, "Devices")
        TabbedWindow(root, app, on_close=_quit, initial_tab=initial_tab)
    elif kind == "pairing":
        PairingWindow(root, app, on_close=_quit)
    elif kind == "devices":
        DevicesWindow(root, app, on_close=_quit)
    elif kind == "settings":
        SettingsWindow(root, app, on_close=_quit)
    elif kind == "logs":
        LogsWindow(root, on_close=_quit)
    elif kind == "incoming":
        IncomingWindow(root, app, on_close=_quit)
    elif kind == "history":
        HistoryWindow(root, app, on_close=_quit)
    elif kind == "file_picker":
        # Lightweight path: open a native file dialog, emit the result, done.
        # No CTk window needed -- just a hidden Tk root to host the dialog.
        import tkinter as tk
        from tkinter import filedialog

        root.destroy()  # discard the CTk root, use a plain Tk one
        tk_root = tk.Tk()
        tk_root.withdraw()
        path = filedialog.askopenfilename(
            title="Select file to send",
            parent=tk_root,
        )
        tk_root.destroy()
        if path:
            _emit("file_selected", path=path)
        return 0
    else:
        log.error("Unknown window: %s", window_name)
        return 1

    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(_run_child(name))
