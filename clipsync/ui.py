"""CustomTkinter UI: pairing, devices, settings windows.

All UI work must happen on the Tk main thread. The `UIManager` exposes a
thread-safe `schedule(callable)` that non-UI threads (tray, pairing
accepter) use to request windows. The manager owns a hidden root
`CTk` instance whose mainloop drives every Toplevel.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk
from PIL import Image

from . import config, pairing
from .autostart import is_autostart_enabled, set_autostart
from .syncthing import SyncthingClient

log = logging.getLogger(__name__)


def _center_window(window: ctk.CTkToplevel | ctk.CTk, width: int, height: int) -> None:
    window.update_idletasks()
    sw = window.winfo_screenwidth()
    sh = window.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


class UIManager:
    """Owns the Tk root, marshals calls from other threads, tracks windows."""

    def __init__(self) -> None:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("dark-blue")
        self._root: ctk.CTk | None = None
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._pairing_window: PairingWindow | None = None
        self._settings_window: SettingsWindow | None = None
        self._devices_window: DevicesWindow | None = None
        self._logs_window: LogsWindow | None = None
        self._on_exit: Callable[[], None] | None = None

    def create_root(self) -> ctk.CTk:
        if self._root is not None:
            return self._root
        root = ctk.CTk()
        root.title(config.APP_NAME)
        root.withdraw()
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._root = root
        self._drain_queue()
        return root

    def mainloop(self, on_exit: Callable[[], None] | None = None) -> None:
        self._on_exit = on_exit
        root = self.create_root()
        root.mainloop()
        if on_exit:
            on_exit()

    def _drain_queue(self) -> None:
        assert self._root is not None
        try:
            while True:
                fn = self._queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("UI task raised")
        except queue.Empty:
            pass
        self._root.after(100, self._drain_queue)

    def schedule(self, fn: Callable[[], None]) -> None:
        """Safe from any thread."""
        self._queue.put(fn)

    def quit(self) -> None:
        def _do_quit() -> None:
            for w in (self._pairing_window, self._settings_window, self._devices_window, self._logs_window):
                if w is not None and w.exists():
                    w.close()
            if self._root is not None:
                self._root.quit()
                self._root.destroy()
                self._root = None

        self.schedule(_do_quit)

    def open_pairing(self, app: AppContext) -> None:
        def _open() -> None:
            if self._pairing_window is not None and self._pairing_window.exists():
                self._pairing_window.focus()
                return
            assert self._root is not None
            self._pairing_window = PairingWindow(self._root, app, on_close=self._on_pairing_closed)

        self.schedule(_open)

    def _on_pairing_closed(self) -> None:
        self._pairing_window = None

    def open_settings(self, app: AppContext) -> None:
        def _open() -> None:
            if self._settings_window is not None and self._settings_window.exists():
                self._settings_window.focus()
                return
            assert self._root is not None
            self._settings_window = SettingsWindow(self._root, app, on_close=self._on_settings_closed, ui=self)

        self.schedule(_open)

    def _on_settings_closed(self) -> None:
        self._settings_window = None

    def open_devices(self, app: AppContext) -> None:
        def _open() -> None:
            if self._devices_window is not None and self._devices_window.exists():
                self._devices_window.focus()
                return
            assert self._root is not None
            self._devices_window = DevicesWindow(self._root, app, on_close=self._on_devices_closed)

        self.schedule(_open)

    def _on_devices_closed(self) -> None:
        self._devices_window = None

    def open_logs(self) -> None:
        def _open() -> None:
            if self._logs_window is not None and self._logs_window.exists():
                self._logs_window.focus()
                return
            assert self._root is not None
            self._logs_window = LogsWindow(self._root, on_close=self._on_logs_closed)

        self.schedule(_open)

    def _on_logs_closed(self) -> None:
        self._logs_window = None


class AppContext:
    """Everything a window needs to talk to the running app.

    Passed in by main.py; the UI classes treat it as an opaque bag of
    callbacks rather than importing the app module directly."""

    def __init__(
        self,
        settings: config.Settings,
        client: SyncthingClient,
        device_id: str,
        on_pause_changed: Callable[[bool], None],
        on_reset: Callable[[], None],
        on_folder_changed: Callable[[str], None],
    ) -> None:
        self.settings = settings
        self.client = client
        self.device_id = device_id
        self.on_pause_changed = on_pause_changed
        self.on_reset = on_reset
        self.on_folder_changed = on_folder_changed


class _BaseWindow:
    """Shared Toplevel behavior: fixed size, centered, accent-styled title."""

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
        self.window.after(50, lambda: self.window.lift())

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


class PairingWindow(_BaseWindow):
    """QR code of our device ID + manual entry + webcam scan."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Add Device", config.PAIRING_WINDOW_SIZE, on_close)
        self._app = app
        self._scanner: pairing.WebcamQRScanner | None = None
        self._status_var = ctk.StringVar(value="Scan this QR on your other device, or paste its ID below.")

        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        title = ctk.CTkLabel(container, text="Pair a device", font=ctk.CTkFont(size=18, weight="bold"))
        title.pack(pady=(0, 12))

        self._qr_label = ctk.CTkLabel(container, text="")
        self._qr_label.pack()
        self._render_qr(app.device_id)

        id_label = ctk.CTkLabel(
            container,
            text=app.device_id,
            font=ctk.CTkFont(size=10),
            wraplength=360,
            justify="center",
        )
        id_label.pack(pady=(6, 10))
        id_label.bind("<Button-1>", lambda _e: self._copy_to_clipboard(app.device_id))

        copy_btn = ctk.CTkButton(
            container,
            text="Copy device ID",
            fg_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=lambda: self._copy_to_clipboard(app.device_id),
        )
        copy_btn.pack(fill="x", pady=(0, 12))

        entry_row = ctk.CTkFrame(container, fg_color="transparent")
        entry_row.pack(fill="x", pady=(0, 8))
        self._entry = ctk.CTkEntry(entry_row, placeholder_text="Paste remote device ID")
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        add_btn = ctk.CTkButton(
            entry_row,
            text="Add",
            width=70,
            fg_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._on_add_clicked,
        )
        add_btn.pack(side="left")

        scan_btn = ctk.CTkButton(
            container,
            text="Scan QR with webcam",
            fg_color="transparent",
            border_width=1,
            border_color=config.ACCENT_COLOR,
            text_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._on_scan_clicked,
        )
        scan_btn.pack(fill="x")

        status = ctk.CTkLabel(
            container,
            textvariable=self._status_var,
            wraplength=360,
            justify="center",
            font=ctk.CTkFont(size=11),
        )
        status.pack(pady=(14, 0))

    def _render_qr(self, device_id: str) -> None:
        qr_img = pairing.generate_qr(device_id, box_size=6, border=2)
        qr_img = qr_img.resize((220, 220), Image.NEAREST)
        ctk_img = ctk.CTkImage(light_image=qr_img, dark_image=qr_img, size=(220, 220))
        self._qr_label.configure(image=ctk_img)
        self._qr_label.image = ctk_img  # keep reference

    def _copy_to_clipboard(self, value: str) -> None:
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(value)
            self._status_var.set("Device ID copied to clipboard.")
        except Exception:
            log.exception("Clipboard copy failed")

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
        try:
            pairing.pair_with_device(self._app.client, device_id)
            self.window.after(0, lambda: self._status_var.set(f"Waiting for {device_id[:7]} to accept…"))
            self.window.after(0, self._start_pending_watch, device_id)
        except Exception as exc:
            log.exception("Pairing failed")
            err = str(exc)
            self.window.after(0, lambda: self._status_var.set(f"Failed to pair: {err}"))

    def _set_pending(self, device_id: str) -> None:
        self._status_var.set(f"Adding {device_id[:7]}…")

    def _start_pending_watch(self, device_id: str) -> None:
        """After we request pairing, poll connections until the device shows up."""

        def check() -> None:
            try:
                devices = self._app.client.connected_devices()
            except Exception:
                self.window.after(2000, check)
                return
            match = next((d for d in devices if d["deviceID"] == device_id), None)
            if match and match["connected"]:
                self._status_var.set(f"Connected to {device_id[:7]}! Clipboard will sync now.")
                return
            if not self.exists():
                return
            self.window.after(2000, check)

        self.window.after(2000, check)

    def _on_scan_clicked(self) -> None:
        if self._scanner is not None:
            return
        self._status_var.set("Opening camera… point it at the QR on the other device.")
        scanner = pairing.WebcamQRScanner(on_detected=self._on_qr_detected)
        self._scanner = scanner
        scanner.start()

    def _on_qr_detected(self, device_id: str) -> None:
        def apply() -> None:
            self._status_var.set(f"Scanned {device_id[:7]}, pairing…")
            if self._scanner is not None:
                self._scanner.stop()
                self._scanner = None
            self._set_pending(device_id)
            threading.Thread(target=self._pair_worker, args=(device_id,), daemon=True).start()

        self.window.after(0, apply)

    def close(self) -> None:
        if self._scanner is not None:
            try:
                self._scanner.stop()
            except Exception:
                pass
            self._scanner = None
        super().close()


class DevicesWindow(_BaseWindow):
    """List of paired devices with live connection status."""

    def __init__(self, parent: ctk.CTk, app: AppContext, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Connected Devices", (420, 420), on_close)
        self._app = app

        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(container, text="Connected devices", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(0, 10))

        self._list_frame = ctk.CTkScrollableFrame(container, fg_color=("gray90", "gray17"))
        self._list_frame.pack(fill="both", expand=True)

        refresh_btn = ctk.CTkButton(
            container,
            text="Refresh",
            fg_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._refresh,
        )
        refresh_btn.pack(fill="x", pady=(12, 0))

        self._refresh()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if not self.exists():
            return
        self.window.after(3000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if not self.exists():
            return
        self._refresh()
        self._schedule_refresh()

    def _refresh(self) -> None:
        for child in self._list_frame.winfo_children():
            child.destroy()
        try:
            devices = self._app.client.connected_devices()
        except Exception as exc:
            ctk.CTkLabel(self._list_frame, text=f"Error: {exc}", text_color="red").pack(pady=10)
            return
        if not devices:
            ctk.CTkLabel(
                self._list_frame,
                text="No devices paired yet.\nUse Add Device to pair one.",
                font=ctk.CTkFont(size=12),
                justify="center",
            ).pack(pady=30)
            return
        for d in devices:
            self._build_row(d)

    def _build_row(self, device: dict) -> None:
        row = ctk.CTkFrame(self._list_frame, fg_color=("gray85", "gray22"))
        row.pack(fill="x", padx=4, pady=4)
        row.grid_columnconfigure(0, weight=1)

        name_text = device.get("name") or device["deviceID"][:7]
        name_lbl = ctk.CTkLabel(row, text=name_text, font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        name_lbl.grid(row=0, column=0, sticky="we", padx=10, pady=(8, 0))

        short_id = device["deviceID"][:24] + "…"
        id_lbl = ctk.CTkLabel(row, text=short_id, font=ctk.CTkFont(size=10), anchor="w")
        id_lbl.grid(row=1, column=0, sticky="we", padx=10, pady=(0, 8))

        status_color = "#2E8B57" if device["connected"] else ("gray50", "gray60")
        status_text = "● Connected" if device["connected"] else "○ Offline"
        status_lbl = ctk.CTkLabel(row, text=status_text, text_color=status_color, font=ctk.CTkFont(size=11))
        status_lbl.grid(row=0, column=1, rowspan=2, padx=10)

        remove_btn = ctk.CTkButton(
            row,
            text="Remove",
            width=70,
            fg_color="transparent",
            border_width=1,
            text_color=("gray30", "gray80"),
            hover_color=("gray75", "gray30"),
            command=lambda did=device["deviceID"]: self._remove_device(did),
        )
        remove_btn.grid(row=0, column=2, rowspan=2, padx=(0, 10))

    def _remove_device(self, device_id: str) -> None:
        try:
            self._app.client.remove_device(device_id)
        except Exception:
            log.exception("Failed to remove device")
        self._refresh()


class SettingsWindow(_BaseWindow):
    """Toggles for autostart, notifications, pause, sync folder, reset."""

    def __init__(
        self,
        parent: ctk.CTk,
        app: AppContext,
        on_close: Callable[[], None],
        ui: UIManager,
    ) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Settings", config.SETTINGS_WINDOW_SIZE, on_close)
        self._app = app
        self._ui = ui

        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(container, text="Settings", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", pady=(0, 12)
        )

        self._autostart_var = ctk.BooleanVar(value=is_autostart_enabled())
        autostart_sw = ctk.CTkSwitch(
            container,
            text="Start on login",
            variable=self._autostart_var,
            command=self._on_autostart_toggle,
            progress_color=config.ACCENT_COLOR,
        )
        autostart_sw.pack(anchor="w", pady=4)

        self._notify_var = ctk.BooleanVar(value=bool(app.settings.get("show_notifications")))
        notify_sw = ctk.CTkSwitch(
            container,
            text="Show notifications on sync",
            variable=self._notify_var,
            command=self._on_notify_toggle,
            progress_color=config.ACCENT_COLOR,
        )
        notify_sw.pack(anchor="w", pady=4)

        self._pause_var = ctk.BooleanVar(value=bool(app.settings.get("sync_paused")))
        pause_sw = ctk.CTkSwitch(
            container,
            text="Sync paused",
            variable=self._pause_var,
            command=self._on_pause_toggle,
            progress_color=config.ACCENT_COLOR,
        )
        pause_sw.pack(anchor="w", pady=4)

        ctk.CTkLabel(container, text="Sync folder path (advanced)", font=ctk.CTkFont(size=11)).pack(
            anchor="w", pady=(14, 2)
        )
        folder_row = ctk.CTkFrame(container, fg_color="transparent")
        folder_row.pack(fill="x")
        self._folder_entry = ctk.CTkEntry(folder_row)
        self._folder_entry.insert(0, str(app.settings.get("sync_folder") or config.SYNC_FOLDER))
        self._folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        save_btn = ctk.CTkButton(
            folder_row,
            text="Save",
            width=70,
            fg_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._on_save_folder,
        )
        save_btn.pack(side="left")

        logs_btn = ctk.CTkButton(
            container,
            text="View Syncthing logs",
            fg_color="transparent",
            border_width=1,
            text_color=config.ACCENT_COLOR,
            border_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._on_view_logs,
        )
        logs_btn.pack(fill="x", pady=(18, 6))

        reset_btn = ctk.CTkButton(
            container,
            text="Reset / unpair all devices",
            fg_color="#9b2c2c",
            hover_color="#7a2222",
            command=self._on_reset,
        )
        reset_btn.pack(fill="x", pady=(0, 6))

        self._status = ctk.CTkLabel(container, text="", font=ctk.CTkFont(size=11))
        self._status.pack(pady=(8, 0))

    def _on_autostart_toggle(self) -> None:
        enabled = bool(self._autostart_var.get())
        set_autostart(enabled)
        self._app.settings.set("start_on_login", enabled)
        self._status.configure(text=f"Start on login {'enabled' if enabled else 'disabled'}.")

    def _on_notify_toggle(self) -> None:
        self._app.settings.set("show_notifications", bool(self._notify_var.get()))

    def _on_pause_toggle(self) -> None:
        paused = bool(self._pause_var.get())
        self._app.settings.set("sync_paused", paused)
        self._app.on_pause_changed(paused)
        self._status.configure(text=f"Sync {'paused' if paused else 'resumed'}.")

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
        self._ui.open_logs()

    def _on_reset(self) -> None:
        confirm = ctk.CTkToplevel(self.window)
        confirm.title("Confirm reset")
        confirm.resizable(False, False)
        _center_window(confirm, 320, 140)
        ctk.CTkLabel(
            confirm,
            text="Remove all paired devices?\nYou will need to re-pair them.",
            justify="center",
        ).pack(padx=20, pady=(20, 10))
        btns = ctk.CTkFrame(confirm, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(0, 16))

        def do_reset() -> None:
            confirm.destroy()
            self._app.on_reset()
            self._status.configure(text="All devices removed.")

        ctk.CTkButton(btns, text="Cancel", fg_color="transparent", border_width=1, command=confirm.destroy).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        ctk.CTkButton(btns, text="Reset", fg_color="#9b2c2c", hover_color="#7a2222", command=do_reset).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )


class LogsWindow(_BaseWindow):
    """Read-only tail of the ClipSync log file."""

    def __init__(self, parent: ctk.CTk, on_close: Callable[[], None]) -> None:
        super().__init__(parent, f"{config.APP_NAME} — Logs", (600, 400), on_close)
        container = ctk.CTkFrame(self.window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=16, pady=16)
        self._textbox = ctk.CTkTextbox(container, wrap="none", font=ctk.CTkFont(family="Menlo", size=11))
        self._textbox.pack(fill="both", expand=True)
        self._refresh()
        refresh = ctk.CTkButton(
            container,
            text="Refresh",
            fg_color=config.ACCENT_COLOR,
            hover_color=config.ACCENT_HOVER,
            command=self._refresh,
        )
        refresh.pack(fill="x", pady=(10, 0))

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
