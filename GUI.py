"""Live status GUI for Precilaser amplifiers.

Reads ports and friendly names from devices.json, connects to each amplifier,
and shows streaming 0x44 status packets in a wrapping grid of cards.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Optional

import serial
from serial import SerialException

BYTEORDER = "big"
BAUD = 115200
DEVICES_PATH = Path(__file__).with_name("devices.json")
CARD_WIDTH = 240
CARD_PAD = 8
UI_POLL_MS = 200
# Amps stream 0x44 continuously; treat silence longer than this as disconnected.
STALE_TIMEOUT_S = 3.0
PD_COL_WIDTH = 58
PD_COL_HEIGHT = 54
TEMP_COL_WIDTH = 40
TEMP_COL_HEIGHT = 36
STAGE_COL_WIDTH = 52
STAGE_COL_HEIGHT = 36
METRIC_COL_PAD = 2

CMD_ENABLE_DRIVER = 0x30
CMD_SET_CURRENT = 0xA1
DRIVER_DISABLE_ALL = 0x00
MAX_DRIVERS = 3
DEFAULT_DRIVERS = 3
CMD_GAP_S = 0.25
CURRENT_RAMP_STEP_A = 0.5
CURRENT_RAMP_INTERVAL_S = 1.0

COLOR_OK = "#1b7f2a"
COLOR_ERR = "#c62828"
COLOR_DIM = "#9a9a9a"
COLOR_UNUSED = "#bdbdbd"

BASE_FONT_SIZE = 9
FONT_SIZE_STEP = 1
FONT_SIZE_MIN = 7
FONT_SIZE_MAX = 18


CURRENT_ENTRY_WIDTH = 7


def scaled_px(base: int, font_size: int) -> int:
    """Scale a layout pixel size with the current UI font size."""
    return max(1, int(round(base * font_size / BASE_FONT_SIZE)))


@dataclass
class AmpStatus:
    connected: bool = False
    error: str = ""
    connected_at: float = 0.0
    last_update: float = 0.0
    checksum_ok: bool = False
    stable: bool = False
    system_status: int = 0
    driver_status: int = 0
    currents: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    pd_values: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    pd_status: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    temperatures: list[float] = field(default_factory=lambda: [0.0] * 5)


def format_pd_status(status: int) -> tuple[str, str]:
    """Return (label, kind) where kind is 'ok' or 'err'.

    D5 = upper-limit event (HIGH), D6 = lower-limit event (LOW).
    OK status shows an empty label so only faults are visible.
    """
    high = bool(status >> 5 & 1)
    low = bool(status >> 6 & 1)
    if low and high:
        return "LOW/HIGH", "err"
    if low:
        return "LOW", "err"
    if high:
        return "HIGH", "err"
    return "", "ok"


# flag_system_status bits: 0 = normal/OK, 1 = alarm/fault. Spares omitted.
PROTECTION_PD_FLAGS = (
    ("PD1", 4),
    ("PD2", 5),
    ("PD3", 6),
    ("PD4", 7),
)
PROTECTION_TEMP_FLAGS = (
    ("T1", 8),
    ("T2", 9),
    ("T3", 10),
    ("T4", 11),
    ("T5", 12),
)
PROTECTION_FLAGS = PROTECTION_PD_FLAGS + PROTECTION_TEMP_FLAGS

PROT_COL_WIDTH = 30
PROT_COL_HEIGHT = 20
PROT_COL_PAD = 1


def clamp_driver_count(value) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = DEFAULT_DRIVERS
    return max(1, min(MAX_DRIVERS, count))


def driver_enable_mask(driver_count: int) -> int:
    return (1 << clamp_driver_count(driver_count)) - 1


def protection_kind(system_status: int, bit: int) -> str:
    return "ok" if (system_status >> bit & 1) == 0 else "err"


def driver_stage_kind(driver_status: int, stage: int) -> str:
    """Color Stage N from Driver_unlock 'has enabled' flags D3/D4/D5."""
    return "ok" if (driver_status >> (3 + stage) & 1) else "err"


def drivers_all_enabled(driver_status: int, driver_count: int) -> bool:
    mask = driver_enable_mask(driver_count)
    return (driver_status >> 3 & mask) == mask


def has_protection_errors(system_status: int) -> bool:
    return any((system_status >> bit) & 1 for _name, bit in PROTECTION_FLAGS)


def can_set_current(status: AmpStatus, driver_count: int) -> tuple[bool, str]:
    if not status.connected or status.last_update == 0:
        return False, "Not connected"
    if not drivers_all_enabled(status.driver_status, driver_count):
        return False, "All drivers must be enabled"
    if has_protection_errors(status.system_status):
        return False, "Interlock fault active"
    return True, ""


def build_frame(command: int, data: bytes = b"") -> bytes:
    payload = bytes([0x00, 0x00, command, len(data)]) + data
    checksum_sum = sum(payload) & 0xFF
    checksum_xor = 0
    for b in payload:
        checksum_xor ^= b
    return b"\x50" + payload + bytes([checksum_sum, checksum_xor, 0x0D, 0x0A])


def build_set_current_frame(amps: float) -> bytes:
    value = max(0, int(round(amps * 100)))
    return build_frame(CMD_SET_CURRENT, value.to_bytes(2, BYTEORDER))


def load_devices(path: Path) -> list[dict]:
    """Return amplifiers in devices.json order (array order is preserved)."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        devices = data
    else:
        amplifiers = data.get("amplifiers", [])
        if isinstance(amplifiers, dict):
            # Insertion order if someone used an object instead of an array.
            devices = [
                {**value, "name": value.get("name", key)}
                if isinstance(value, dict)
                else {"name": key, "port": value}
                for key, value in amplifiers.items()
            ]
        else:
            devices = list(amplifiers)
    return list(devices)


def parse_status_frame(frame: bytes) -> Optional[AmpStatus]:
    if len(frame) < 9 or frame[0] != 0x50 or frame[-2:] != b"\r\n":
        return None

    command = frame[3]
    n = frame[4]
    if command != 0x44 or n != 64:
        return None

    data = frame[5 : 5 + n]
    if len(data) != 64:
        return None

    checksum_data = frame[1:-4]
    sum_ok = frame[-4] == (sum(checksum_data) & 0xFF)
    xor = 0
    for x in checksum_data:
        xor ^= x
    xor_ok = frame[-3] == xor

    u16 = lambda i: int.from_bytes(data[i : i + 2], BYTEORDER)

    return AmpStatus(
        connected=True,
        checksum_ok=sum_ok and xor_ok,
        stable=bool(data[0]),
        system_status=u16(2),
        driver_status=data[4],
        currents=[u16(7) / 100, u16(14) / 100, u16(21) / 100],
        pd_values=[u16(28), u16(30), u16(32), u16(34)],
        pd_status=list(data[36:40]),
        temperatures=[
            u16(42) / 100,
            u16(44) / 100,
            u16(46) / 100,
            u16(48) / 100,
            u16(50) / 100,
        ],
        last_update=time.time(),
    )


class AmplifierReader(threading.Thread):
    def __init__(self, device_name: str, port: str, order: int, driver_count: int = DEFAULT_DRIVERS):
        super().__init__(name=f"amp-{port}", daemon=True)
        self.device_name = device_name
        self.port = port
        self.order = order
        self.driver_count = clamp_driver_count(driver_count)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = AmpStatus(error="Connecting…")
        self._tx: queue.Queue[bytes] = queue.Queue()

    def get_status(self) -> AmpStatus:
        with self._lock:
            return AmpStatus(**self._status.__dict__)

    def _set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._status, k, v)

    def stop(self) -> None:
        self._stop.set()

    def send_driver_enable(self, enable: bool) -> None:
        mask = driver_enable_mask(self.driver_count) if enable else DRIVER_DISABLE_ALL
        self._tx.put(build_frame(CMD_ENABLE_DRIVER, bytes([mask])))

    def send_set_current(self, amps: float) -> None:
        self._tx.put(build_set_current_frame(amps))

    def run(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            ser = None
            try:
                ser = serial.Serial(self.port, BAUD, timeout=0.2)
                self._set(connected=True, error="", connected_at=time.time())
                buf.clear()

                while not self._stop.is_set():
                    try:
                        while True:
                            frame = self._tx.get_nowait()
                            ser.write(frame)
                            time.sleep(CMD_GAP_S)
                    except queue.Empty:
                        pass

                    chunk = ser.read(ser.in_waiting or 1)
                    if chunk:
                        buf += chunk

                    while len(buf) >= 5:
                        if buf[0] != 0x50:
                            del buf[0]
                            continue

                        n = buf[4]
                        frame_len = 5 + n + 4
                        if len(buf) < frame_len:
                            break

                        frame = bytes(buf[:frame_len])
                        del buf[:frame_len]

                        status = parse_status_frame(frame)
                        if status is not None:
                            with self._lock:
                                self._status = status

            except SerialException as exc:
                self._set(connected=False, error=str(exc), connected_at=0.0)
                self._stop.wait(2.0)
            except Exception as exc:  # noqa: BLE001 — keep reader alive
                self._set(connected=False, error=str(exc), connected_at=0.0)
                self._stop.wait(2.0)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass


class FlowFrame(ttk.Frame):
    """Children wrap to the next row when the container is resized."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._cards: list[tk.Widget] = []
        self.card_min_width = CARD_WIDTH
        self.bind("<Configure>", self._on_configure)

    def add(self, widget: tk.Widget) -> None:
        self._cards.append(widget)
        self.after_idle(self._reflow)

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is self:
            self._reflow()

    def _reflow(self) -> None:
        if not self._cards:
            return

        # Keep JSON / add order: left → right, then next row.
        cards = sorted(
            self._cards,
            key=lambda w: getattr(w, "order", 0),
        )

        # Prefer allocated width; fall back to canvas/parent while first mapping.
        width = self.winfo_width()
        if width <= 1:
            try:
                width = self.master.winfo_width()
            except tk.TclError:
                width = 1
        width = max(width, 1)

        x = CARD_PAD
        y = CARD_PAD
        row_height = 0
        max_bottom = 0

        for card in cards:
            card.update_idletasks()
            cw = max(card.winfo_reqwidth(), self.card_min_width)
            ch = card.winfo_reqheight()

            if x > CARD_PAD and x + cw + CARD_PAD > width:
                x = CARD_PAD
                y += row_height + CARD_PAD
                row_height = 0

            card.place(x=x, y=y, width=cw)
            card.lift()
            x += cw + CARD_PAD
            row_height = max(row_height, ch)
            max_bottom = max(max_bottom, y + ch)

        self.configure(height=max_bottom + CARD_PAD)


class AmplifierCard(ttk.LabelFrame):
    def __init__(self, master, reader: AmplifierReader):
        super().__init__(
            master,
            text=reader.device_name,
            padding=4,
            style="Connecting.TLabelframe",
        )
        self.reader = reader
        self.order = reader.order
        self.driver_count = reader.driver_count
        self._appearance = None
        self._labels: list[ttk.Label] = []
        self._pd_value_labels: list[ttk.Label] = []
        self._pd_status_labels: list[ttk.Label] = []
        self._pd_kinds: list[str] = ["ok"] * 4
        self._prot_labels: list[ttk.Label] = []
        self._prot_kinds: list[str] = ["ok"] * len(PROTECTION_FLAGS)
        self._stage_labels: list[ttk.Label] = []
        self._stage_value_labels: list[ttk.Label] = []
        self._stage_kinds: list[str] = [
            "unused" if i >= self.driver_count else "err" for i in range(MAX_DRIVERS)
        ]
        self._heading_labels: list[ttk.Label] = []
        self._metric_cols: list[dict] = []
        self._dim_body = False
        self._was_live = False

        self.current_vars = [tk.StringVar(value="—") for _ in range(MAX_DRIVERS)]
        self.total_current_var = tk.StringVar(value="Actual Current  —")
        self.pd_vars = [tk.StringVar(value="—") for _ in range(4)]
        self.pd_status_vars = [tk.StringVar(value="") for _ in range(4)]
        self.temp_vars = [tk.StringVar(value="—") for _ in range(5)]

        self._heading("Interlocks", 0)

        prot = ttk.Frame(self)
        prot.grid(row=1, column=0, columnspan=2, sticky="ew")
        prot_pd = ttk.Frame(prot)
        prot_pd.grid(row=0, column=0, sticky="w")
        prot_temp = ttk.Frame(prot)
        prot_temp.grid(row=1, column=0, sticky="w", pady=(4, 0))
        for i, (name, _bit) in enumerate(PROTECTION_PD_FLAGS):
            col = self._metric_column(
                prot_pd,
                i,
                PROT_COL_WIDTH,
                PROT_COL_HEIGHT,
                "prot_pd",
                padx=PROT_COL_PAD,
            )
            lbl = self._label(
                name,
                row=0,
                column=0,
                parent=col,
                track=False,
                style="PdOk.TLabel",
            )
            self._prot_labels.append(lbl)
        for i, (name, _bit) in enumerate(PROTECTION_TEMP_FLAGS):
            col = self._metric_column(
                prot_temp,
                i,
                PROT_COL_WIDTH,
                PROT_COL_HEIGHT,
                "prot_temp",
                padx=PROT_COL_PAD,
            )
            lbl = self._label(
                name,
                row=0,
                column=0,
                parent=col,
                track=False,
                style="PdOk.TLabel",
            )
            self._prot_labels.append(lbl)

        ttk.Separator(self, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=6
        )
        self._heading("Currents [A]", 3)

        currents = ttk.Frame(self)
        currents.grid(row=4, column=0, columnspan=2, sticky="ew")
        for i, var in enumerate(self.current_vars):
            col = self._metric_column(
                currents,
                i,
                STAGE_COL_WIDTH,
                STAGE_COL_HEIGHT,
                "stage",
                padx=METRIC_COL_PAD,
            )
            stage_kind = self._stage_kinds[i]
            stage_style = (
                "Unused.TLabel" if stage_kind == "unused" else "PdErr.TLabel"
            )
            stage_lbl = self._label(
                f"Stage {i + 1}",
                row=0,
                column=0,
                parent=col,
                track=False,
                style=stage_style,
            )
            self._stage_labels.append(stage_lbl)
            value_lbl = self._label(
                var,
                row=1,
                column=0,
                parent=col,
                sticky="w",
                width=5,
                track=False,
                style=stage_style,
            )
            self._stage_value_labels.append(value_lbl)

        ttk.Separator(self, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=6
        )
        self._heading("Photodiodes", 6)

        pds = ttk.Frame(self)
        pds.grid(row=7, column=0, columnspan=2, sticky="ew")
        for i in range(4):
            col = self._metric_column(
                pds, i, PD_COL_WIDTH, PD_COL_HEIGHT, "pd", padx=METRIC_COL_PAD
            )
            self._label(f"PD{i + 1}", row=0, column=0, parent=col)
            value_lbl = self._label(
                self.pd_vars[i],
                row=1,
                column=0,
                parent=col,
                sticky="w",
                track=False,
                style="PdOk.TLabel",
                width=5,
            )
            status_lbl = self._label(
                self.pd_status_vars[i],
                row=2,
                column=0,
                parent=col,
                sticky="w",
                track=False,
                style="PdOk.TLabel",
                width=8,
            )
            self._pd_value_labels.append(value_lbl)
            self._pd_status_labels.append(status_lbl)

        ttk.Separator(self, orient="horizontal").grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=6
        )
        self._heading("Temperatures [°C]", 9)

        temps = ttk.Frame(self)
        temps.grid(row=10, column=0, columnspan=2, sticky="ew")
        for i in range(5):
            col = self._metric_column(
                temps, i, TEMP_COL_WIDTH, TEMP_COL_HEIGHT, "temp", padx=METRIC_COL_PAD
            )
            self._label(f"T{i + 1}", row=0, column=0, parent=col)
            self._label(self.temp_vars[i], row=1, column=0, parent=col, sticky="w", width=5)

        ttk.Separator(self, orient="horizontal").grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=6
        )
        self._heading("Controls", 12)

        controls = ttk.Frame(self)
        controls.grid(row=13, column=0, columnspan=2, sticky="ew")

        total_row = ttk.Frame(controls)
        total_row.grid(row=0, column=0, sticky="w")
        total_lbl = self._label(
            self.total_current_var,
            row=0,
            column=0,
            parent=total_row,
            style="Heading.TLabel",
            track=False,
            padx=(0, 10),
        )
        self._heading_labels.append(total_lbl)

        self.enable_btn = tk.Button(
            total_row,
            text="Enable Driver",
            command=self._toggle_drivers,
            relief="flat",
            bd=0,
            padx=6,
            pady=2,
            fg="white",
            activeforeground="white",
            disabledforeground="#eeeeee",
            cursor="hand2",
        )
        self.enable_btn.grid(row=0, column=1, sticky="w")
        self._set_enable_button_color(COLOR_DIM, active=False)

        entry_row = ttk.Frame(controls)
        entry_row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._label("Set Current [A]", row=0, column=0, parent=entry_row, padx=(0, 6))
        self.current_entry_var = tk.StringVar(value="0.0")
        self.current_entry = ttk.Entry(
            entry_row, textvariable=self.current_entry_var, width=7
        )
        self.current_entry.grid(row=0, column=1)
        self.current_entry.bind("<Return>", lambda _event: self._on_ramp_current())

        self._ramp_stop = threading.Event()
        self._ramp_thread: threading.Thread | None = None

        self.columnconfigure(1, weight=1)

    def _metric_column(
        self,
        parent: ttk.Frame,
        index: int,
        width: int,
        height: int,
        uniform: str,
        padx: int = 8,
    ) -> ttk.Frame:
        """Fixed-size column so neighboring metrics don't shift when text changes."""
        col = ttk.Frame(parent, width=width, height=height)
        col.grid(
            row=0,
            column=index,
            sticky="nw",
            padx=(0 if index == 0 else padx, 0),
        )
        col.grid_propagate(False)
        parent.columnconfigure(index, weight=0, minsize=width, uniform=uniform)
        self._metric_cols.append(
            {
                "frame": col,
                "parent": parent,
                "index": index,
                "base_w": width,
                "base_h": height,
                "uniform": uniform,
            }
        )
        return col

    def apply_layout_scale(self, font_size: int) -> None:
        """Grow/shrink fixed metric cells so larger fonts are not clipped."""
        for m in self._metric_cols:
            w = scaled_px(m["base_w"], font_size)
            h = scaled_px(m["base_h"], font_size)
            m["frame"].configure(width=w, height=h)
            m["parent"].columnconfigure(
                m["index"], weight=0, minsize=w, uniform=m["uniform"]
            )
        self.current_entry.configure(font=("", font_size), width=CURRENT_ENTRY_WIDTH)

    def _label(
        self,
        text_or_var,
        row: int,
        column: int = 0,
        columnspan: int = 1,
        sticky: str = "w",
        pady=0,
        padx=0,
        font=None,
        parent=None,
        justify=None,
        track: bool = True,
        style: str = "Live.TLabel",
        width: int | None = None,
    ) -> ttk.Label:
        kwargs: dict = {"style": style}
        if font is not None:
            kwargs["font"] = font
        if justify is not None:
            kwargs["justify"] = justify
        if width is not None:
            kwargs["width"] = width
        if isinstance(text_or_var, tk.StringVar):
            kwargs["textvariable"] = text_or_var
        else:
            kwargs["text"] = text_or_var
        lbl = ttk.Label(parent or self, **kwargs)
        lbl.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky=sticky,
            pady=pady,
            padx=padx,
        )
        if track:
            self._labels.append(lbl)
        return lbl

    def _heading(self, text: str, row: int) -> None:
        lbl = self._label(
            text, row=row, columnspan=2, style="Heading.TLabel", track=False
        )
        self._heading_labels.append(lbl)

    def _row(self, label: str, var: tk.StringVar, row: int) -> None:
        self._label(label, row=row, column=0, padx=(0, 12))
        self._label(var, row=row, column=1, sticky="e")

    def _log(self, message: str) -> None:
        app = self.winfo_toplevel()
        log_fn = getattr(app, "log", None)
        if callable(log_fn):
            log_fn(f"{self.reader.device_name}: {message}")

    def _set_enable_button_color(self, color: str, active: bool, all_on: bool = False) -> None:
        self.enable_btn.configure(
            text=("Disable Driver" if all_on else "Enable Driver"),
            bg=color,
            activebackground=color,
            highlightbackground=color,
            state=("normal" if active else "disabled"),
        )

    def _toggle_drivers(self) -> None:
        s = self.reader.get_status()
        if not s.connected or s.last_update == 0:
            self._log("Enable/disable blocked — not connected")
            return
        # Toggle from actual laser status for configured drivers only.
        if drivers_all_enabled(s.driver_status, self.driver_count):
            if sum(s.currents) > 0.05:
                self._log("Ramping to 0 A before disabling drivers")
                self._start_ramp(0.0, on_complete=self._disable_after_zero)
            else:
                self.reader.send_driver_enable(False)
        else:
            self.reader.send_driver_enable(True)

    def _disable_after_zero(self) -> None:
        self.reader.send_driver_enable(False)
        self._log("Current near zero — drivers disabled")

    def _parse_current_entry(self) -> float | None:
        try:
            value = float(self.current_entry_var.get().strip())
        except ValueError:
            return None
        if value < 0:
            return None
        return value

    def _on_ramp_current(self) -> None:
        target = self._parse_current_entry()
        if target is None:
            return
        s = self.reader.get_status()
        ok, reason = can_set_current(s, self.driver_count)
        if not ok:
            self._log(f"Current change blocked — {reason.lower()}")
            return
        self._start_ramp(target)

    def _start_ramp(self, target: float, on_complete=None) -> None:
        if self._ramp_thread is not None and self._ramp_thread.is_alive():
            self._log("Ramp blocked — another ramp is already running")
            return
        self._ramp_stop.clear()
        self._ramp_thread = threading.Thread(
            target=self._ramp_worker,
            args=(target, on_complete),
            name=f"ramp-{self.reader.port}",
            daemon=True,
        )
        self._ramp_thread.start()

    def _ramp_worker(self, target: float, on_complete=None) -> None:
        step = CURRENT_RAMP_STEP_A
        while not self._ramp_stop.is_set() and not self.reader._stop.is_set():
            s = self.reader.get_status()
            ok, reason = can_set_current(s, self.driver_count)
            if not ok:
                self._log(f"Ramp aborted — {reason.lower()}")
                return

            current = sum(s.currents)
            if abs(current - target) <= 0.05:
                self.reader.send_set_current(target)
                if on_complete is not None:
                    time.sleep(CURRENT_RAMP_INTERVAL_S)
                    s = self.reader.get_status()
                    if sum(s.currents) <= 0.05:
                        on_complete()
                    else:
                        self._log("Disable blocked — current still above zero")
                return

            if target > current:
                nxt = min(current + step, target)
            else:
                nxt = max(current - step, target)
            nxt = round(nxt, 2)
            self.reader.send_set_current(nxt)
            time.sleep(CURRENT_RAMP_INTERVAL_S)

    def _pd_status_style(self, kind: str) -> str:
        if kind == "unused":
            return "UnusedDim.TLabel" if self._dim_body else "Unused.TLabel"
        if self._dim_body:
            return "PdOkDim.TLabel" if kind == "ok" else "PdErrDim.TLabel"
        return "PdOk.TLabel" if kind == "ok" else "PdErr.TLabel"

    def _apply_pd_styles(self) -> None:
        for value_lbl, status_lbl, kind in zip(
            self._pd_value_labels, self._pd_status_labels, self._pd_kinds
        ):
            style = self._pd_status_style(kind)
            value_lbl.configure(style=style)
            status_lbl.configure(style=style)

    def _apply_prot_styles(self) -> None:
        for lbl, kind in zip(self._prot_labels, self._prot_kinds):
            lbl.configure(style=self._pd_status_style(kind))

    def _apply_stage_styles(self) -> None:
        for name_lbl, value_lbl, kind in zip(
            self._stage_labels, self._stage_value_labels, self._stage_kinds
        ):
            style = self._pd_status_style(kind)
            name_lbl.configure(style=style)
            value_lbl.configure(style=style)

    def _set_appearance(self, kind: str, dim_body: bool) -> None:
        key = (kind, dim_body)
        if key == self._appearance:
            return
        self._appearance = key
        self._dim_body = dim_body
        self.configure(style=f"{kind}.TLabelframe")
        label_style = "Dim.TLabel" if dim_body else "Live.TLabel"
        heading_style = "HeadingDim.TLabel" if dim_body else "Heading.TLabel"
        for lbl in self._labels:
            lbl.configure(style=label_style)
        for lbl in self._heading_labels:
            lbl.configure(style=heading_style)
        self._apply_pd_styles()
        self._apply_prot_styles()
        self._apply_stage_styles()

    def refresh(self) -> None:
        s = self.reader.get_status()
        now = time.time()
        data_fresh = s.last_update > 0 and (now - s.last_update) <= STALE_TIMEOUT_S
        waiting = (
            s.connected
            and s.last_update == 0
            and s.connected_at > 0
            and (now - s.connected_at) <= STALE_TIMEOUT_S
        )

        if data_fresh:
            self._was_live = True
            self._set_appearance("Connected", dim_body=False)
            for i, (_name, bit) in enumerate(PROTECTION_FLAGS):
                self._prot_kinds[i] = protection_kind(s.system_status, bit)
            self._apply_prot_styles()
            for i in range(MAX_DRIVERS):
                if i >= self.driver_count:
                    self._stage_kinds[i] = "unused"
                else:
                    self._stage_kinds[i] = driver_stage_kind(s.driver_status, i)
            self._apply_stage_styles()
            all_on = drivers_all_enabled(s.driver_status, self.driver_count)
            self._set_enable_button_color(
                COLOR_OK if all_on else COLOR_ERR, active=True, all_on=all_on
            )
            for i, v in enumerate(s.currents):
                self.current_vars[i].set(f"{v:.2f}")
            self.total_current_var.set(f"Actual Current  {sum(s.currents):.2f}")
            for i, v in enumerate(s.pd_values):
                self.pd_vars[i].set(str(v))
            for i, v in enumerate(s.pd_status):
                text, kind = format_pd_status(v)
                self.pd_status_vars[i].set(text)
                self._pd_kinds[i] = kind
            self._apply_pd_styles()
            for i, v in enumerate(s.temperatures):
                self.temp_vars[i].set(f"{v:.2f}")
            return

        if waiting:
            self._set_appearance("Connecting", dim_body=False)
            self._set_enable_button_color(COLOR_DIM, active=False)
            return

        if self._was_live:
            self._log("No status for 3 s — marked disconnected, controls disabled")
        self._was_live = False
        self._set_appearance("Disconnected", dim_body=True)
        self._set_enable_button_color(COLOR_DIM, active=False)


def apply_ui_font(root: tk.Misc, size: int) -> None:
    """Apply a uniform UI font size to ttk styles and tk buttons."""
    style = ttk.Style(root)
    regular = ("", size)
    bold = ("", size, "bold")

    style.configure("TLabel", font=regular)
    style.configure("TButton", font=regular)
    style.configure("TEntry", font=regular)
    style.configure("TLabelframe", font=bold)
    style.configure("TLabelframe.Label", font=bold, foreground="#1a1a1a")

    style.configure("Live.TLabel", font=regular, foreground="#1a1a1a")
    style.configure("Dim.TLabel", font=regular, foreground="#9a9a9a")
    style.configure("Heading.TLabel", font=bold, foreground="#1a1a1a")
    style.configure("HeadingDim.TLabel", font=bold, foreground="#9a9a9a")
    style.configure("Connected.TLabelframe.Label", font=bold, foreground="#1a1a1a")
    style.configure("Disconnected.TLabelframe.Label", font=bold, foreground=COLOR_DIM)
    style.configure("Connecting.TLabelframe.Label", font=bold, foreground="#1a1a1a")
    style.configure("PdOk.TLabel", font=regular, foreground="#1b7f2a")
    style.configure("PdErr.TLabel", font=regular, foreground="#c62828")
    style.configure("PdOkDim.TLabel", font=regular, foreground="#8a9a8a")
    style.configure("PdErrDim.TLabel", font=regular, foreground="#b09090")
    style.configure("Unused.TLabel", font=regular, foreground=COLOR_UNUSED)
    style.configure("UnusedDim.TLabel", font=regular, foreground="#d0d0d0")

    def _walk(widget: tk.Misc) -> None:
        if isinstance(widget, (tk.Button, tk.Text)):
            widget.configure(font=regular)
        for child in widget.winfo_children():
            _walk(child)

    _walk(root)


class App(tk.Tk):
    def __init__(self, devices: list[dict]):
        super().__init__()
        self.title("Precilaser Amplifiers")
        self.geometry("980x720")
        self.minsize(360, 400)
        self._font_size = BASE_FONT_SIZE
        apply_ui_font(self, self._font_size)

        menubar = tk.Menu(self)
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(
            label="Larger Text",
            command=self._font_larger,
            accelerator="Ctrl++",
        )
        view_menu.add_command(
            label="Smaller Text",
            command=self._font_smaller,
            accelerator="Ctrl+-",
        )
        menubar.add_cascade(label="View", menu=view_menu)
        self.config(menu=menubar)
        self.bind_all("<Control-plus>", lambda _e: self._font_larger())
        self.bind_all("<Control-equal>", lambda _e: self._font_larger())
        self.bind_all("<Control-minus>", lambda _e: self._font_smaller())

        outer = ttk.Frame(self, padding=6)
        outer.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(outer, text="Log", padding=4)
        log_frame.pack(side="bottom", fill="x", pady=(6, 0))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical")
        self._log = tk.Text(
            log_frame,
            height=6,
            wrap="word",
            state="disabled",
            relief="flat",
            yscrollcommand=log_scroll.set,
        )
        log_scroll.configure(command=self._log.yview)
        log_scroll.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)

        cards_area = ttk.Frame(outer)
        cards_area.pack(fill="both", expand=True)

        canvas = tk.Canvas(cards_area, highlightthickness=0)
        scrollbar = ttk.Scrollbar(cards_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.flow = FlowFrame(canvas)
        self._window = canvas.create_window((0, 0), window=self.flow, anchor="nw")

        def _on_canvas_configure(event: tk.Event) -> None:
            if event.widget is not canvas:
                return
            canvas.itemconfigure(self._window, width=event.width)
            self.flow._reflow()
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_flow_configure(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        # add='+' so we don't replace FlowFrame's own <Configure> → _reflow binding
        self.flow.bind("<Configure>", _on_flow_configure, add="+")
        canvas.bind("<Configure>", _on_canvas_configure)

        self.readers: list[AmplifierReader] = []
        self.cards: list[AmplifierCard] = []

        if not devices:
            ttk.Label(
                self.flow,
                text="No amplifiers in devices.json",
                padding=20,
            ).pack()
        else:
            for index, entry in enumerate(devices):
                name = str(entry.get("name", "Amplifier"))
                port = str(entry.get("port", ""))
                driver_count = clamp_driver_count(
                    entry.get("current drivers", DEFAULT_DRIVERS)
                )
                reader = AmplifierReader(
                    name, port, order=index, driver_count=driver_count
                )
                reader.start()
                self.readers.append(reader)

                card = AmplifierCard(self.flow, reader)
                self.flow.add(card)
                self.cards.append(card)

        # Re-apply after cards exist so tk.Button fonts match ttk styles.
        self._apply_font_size()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(UI_POLL_MS, self._tick)

    def log(self, message: str) -> None:
        """Append a timestamped line to the bottom log (thread-safe)."""

        def _append() -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            self._log.configure(state="normal")
            self._log.insert("end", f"[{stamp}] {message}\n")
            self._log.see("end")
            self._log.configure(state="disabled")

        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.after(0, _append)

    def _apply_font_size(self) -> None:
        apply_ui_font(self, self._font_size)
        for card in self.cards:
            card.apply_layout_scale(self._font_size)
        self.flow.card_min_width = scaled_px(CARD_WIDTH, self._font_size)
        self.flow._reflow()

    def _font_larger(self) -> None:
        if self._font_size >= FONT_SIZE_MAX:
            return
        self._font_size += FONT_SIZE_STEP
        self._apply_font_size()

    def _font_smaller(self) -> None:
        if self._font_size <= FONT_SIZE_MIN:
            return
        self._font_size -= FONT_SIZE_STEP
        self._apply_font_size()

    def _tick(self) -> None:
        for card in self.cards:
            card.refresh()
        self.after(UI_POLL_MS, self._tick)

    def _on_close(self) -> None:
        for reader in self.readers:
            reader.stop()
        self.destroy()


def main() -> None:
    if not DEVICES_PATH.exists():
        raise SystemExit(f"Missing {DEVICES_PATH}")

    devices = load_devices(DEVICES_PATH)
    app = App(devices)
    app.mainloop()


if __name__ == "__main__":
    main()