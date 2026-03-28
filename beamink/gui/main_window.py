from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from beamink.config import AppConfig
from beamink.core.controller import LaserController
from beamink.core.types import ControllerSnapshot


class MainWindow:
    def __init__(self, root: tk.Tk, controller: LaserController, config: AppConfig) -> None:
        self.root = root
        self.controller = controller
        self.config = config

        self.root.title("BeamInk")
        self.root.geometry("1220x760")
        self.root.attributes("-topmost", self.config.ui.stay_on_top)
        if self.config.ui.fullscreen_on_start:
            self.root.attributes("-fullscreen", True)
        if self.config.ui.hide_cursor:
            self.root.configure(cursor="none")
        self.root.bind("<Escape>", self._exit_fullscreen)
        self.root.bind("<F11>", self._toggle_fullscreen)

        self.debug_var = tk.BooleanVar(value=True)
        self.armed_var = tk.BooleanVar(value=False)
        self.pen_up_mode_var = tk.StringVar(value=self.config.pressure.pen_up_mode)
        self.hover_power_var = tk.StringVar(value=f"{self.config.pressure.hover_power_pct:.1f}")
        self.floor_power_var = tk.StringVar(value=f"{self.config.pressure.contact_power_floor_pct:.1f}")
        self.ceil_power_var = tk.StringVar(value=f"{self.config.pressure.contact_power_ceil_pct:.1f}")
        self.feed_rate_var = tk.StringVar(value=str(self.config.transport.feed_rate))

        self.status_var = tk.StringVar(value="Starting")
        self.device_var = tk.StringVar(value="No Wacom device yet")
        self.capture_var = tk.StringVar(value="Unknown")
        self.position_var = tk.StringVar(value="0.0 mm, 0.0 mm")
        self.power_var = tk.StringVar(value="0.0%")
        self.transport_var = tk.StringVar(value=self.controller.get_snapshot().transport_name)
        self.error_var = tk.StringVar(value="")

        self._build_layout()
        self._poll_snapshot()

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=3)
        self.root.columnconfigure(1, weight=2)
        self.root.rowconfigure(0, weight=1)

        preview_frame = ttk.Frame(self.root, padding=12)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        ttk.Label(preview_frame, text="Stage Preview", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.canvas = tk.Canvas(
            preview_frame,
            width=self.config.ui.stage_canvas_px,
            height=self.config.ui.stage_canvas_px,
            bg="#f8f8f5",
            highlightthickness=1,
            highlightbackground="#a7a7a0",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        side = ttk.Frame(self.root, padding=(0, 12, 12, 12))
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(2, weight=1)

        status = ttk.LabelFrame(side, text="Status", padding=12)
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)

        _status_row(status, 0, "Controller", self.status_var)
        _status_row(status, 1, "Device", self.device_var)
        _status_row(status, 2, "Tablet Capture", self.capture_var)
        _status_row(status, 3, "Stage", self.position_var)
        _status_row(status, 4, "Power", self.power_var)
        _status_row(status, 5, "Transport", self.transport_var)
        _status_row(status, 6, "Last Error", self.error_var)

        controls = ttk.LabelFrame(side, text="Controls", padding=12)
        controls.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        controls.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            controls,
            text="Arm live output",
            variable=self.armed_var,
            command=self._toggle_armed,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Button(controls, text="Stop Session", command=self._stop_processing).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        ttk.Button(controls, text="Clear Preview Path", command=self.controller.clear_path).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        ttk.Button(controls, text="Refresh Machine Info", command=self._refresh_machine_info).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )

        ttk.Label(controls, text="Pen-up mode").grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            controls,
            values=["preview", "off"],
            textvariable=self.pen_up_mode_var,
            state="readonly",
        ).grid(row=4, column=1, sticky="ew", pady=(12, 0))

        ttk.Label(controls, text="Hover power %").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.hover_power_var).grid(row=5, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(controls, text="Contact floor %").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.floor_power_var).grid(row=6, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(controls, text="Contact ceiling %").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.ceil_power_var).grid(row=7, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(controls, text="Contact feed mm/min").grid(row=8, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.feed_rate_var).grid(row=8, column=1, sticky="ew", pady=(8, 0))

        ttk.Button(controls, text="Apply Output Settings", command=self._apply_power_settings).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )

        ttk.Checkbutton(
            controls,
            text="Show debug log",
            variable=self.debug_var,
            command=self._toggle_debug_log,
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.log_frame = ttk.LabelFrame(side, text="Transport Debug", padding=12)
        self.log_frame.grid(row=2, column=0, sticky="nsew")
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(self.log_frame, wrap="none", height=18, bg="#111111", fg="#f3f3f3")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    def _toggle_armed(self) -> None:
        self.controller.set_armed(self.armed_var.get())

    def _toggle_debug_log(self) -> None:
        if self.debug_var.get():
            self.log_frame.grid()
        else:
            self.log_frame.grid_remove()

    def _stop_processing(self) -> None:
        def worker() -> None:
            self.controller.stop_processing("Manual stop requested")

        threading.Thread(target=worker, name="manual-processing-stop", daemon=True).start()

    def _apply_power_settings(self) -> None:
        try:
            hover = float(self.hover_power_var.get())
            floor = float(self.floor_power_var.get())
            ceil = float(self.ceil_power_var.get())
            feed_rate = int(self.feed_rate_var.get())
        except ValueError:
            messagebox.showerror("Invalid output settings", "Power and feed settings must be numeric.")
            return
        if feed_rate <= 0:
            messagebox.showerror("Invalid output settings", "Contact feed must be greater than zero.")
            return

        self.controller.update_pressure_config(
            hover_power_pct=hover,
            contact_power_floor_pct=floor,
            contact_power_ceil_pct=ceil,
            pen_up_mode=self.pen_up_mode_var.get(),
        )
        self.controller.update_transport_config(feed_rate=feed_rate)

    def _refresh_machine_info(self) -> None:
        def worker() -> None:
            try:
                self.controller.refresh_machine_info()
            except Exception as exc:  # pragma: no cover
                self.root.after(0, lambda: messagebox.showerror("Machine info failed", str(exc)))

        threading.Thread(target=worker, name="machine-info-refresh", daemon=True).start()

    def _poll_snapshot(self) -> None:
        snapshot = self.controller.get_snapshot()
        self._render_snapshot(snapshot)
        self.root.after(self.config.ui.poll_interval_ms, self._poll_snapshot)

    def _render_snapshot(self, snapshot: ControllerSnapshot) -> None:
        self.status_var.set(snapshot.status_text)
        if snapshot.device_name:
            self.device_var.set(f"{snapshot.device_name} [{snapshot.device_path}]")
        self.capture_var.set(snapshot.input_capture_text)
        self.position_var.set(f"{snapshot.stage_x_mm:.2f} mm, {snapshot.stage_y_mm:.2f} mm")
        self.power_var.set(
            f"intended {snapshot.intended_power_pct:.1f}% / output {snapshot.output_power_pct:.1f}%"
        )
        transport_state = snapshot.transport_name
        if snapshot.transport_connected:
            transport_state += " (connected)"
        self.transport_var.set(transport_state)
        self.error_var.set(snapshot.last_error)
        self.armed_var.set(snapshot.armed)

        self._draw_stage(snapshot)
        if self.debug_var.get():
            self._render_log(snapshot)

    def _draw_stage(self, snapshot: ControllerSnapshot) -> None:
        canvas = self.canvas
        canvas.delete("all")

        size = self.config.ui.stage_canvas_px
        pad = 20
        left = pad
        top = pad
        right = size - pad
        bottom = size - pad

        canvas.create_rectangle(left, top, right, bottom, outline="#333333", width=2)
        width_scale = (right - left) / max(self.config.stage.width_mm, 1.0)
        height_scale = (bottom - top) / max(self.config.stage.height_mm, 1.0)

        points = snapshot.path_points
        if len(points) >= 2:
            for prev, curr in zip(points, points[1:]):
                color = "#b53d2a" if curr.contact else "#2a6fb5"
                canvas.create_line(
                    left + prev.x_mm * width_scale,
                    top + prev.y_mm * height_scale,
                    left + curr.x_mm * width_scale,
                    top + curr.y_mm * height_scale,
                    fill=color,
                    width=2,
                )

        x = left + snapshot.stage_x_mm * width_scale
        y = top + snapshot.stage_y_mm * height_scale
        canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#111111")

    def _render_log(self, snapshot: ControllerSnapshot) -> None:
        lines = []
        for entry in snapshot.recent_replies[-8:]:
            lines.append(f"# {entry}")
        lines.extend(snapshot.recent_gcode[-20:])

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", "\n".join(lines))
        self.log_text.configure(state="disabled")

    def _exit_fullscreen(self, _event: tk.Event | None = None) -> None:
        self.root.attributes("-fullscreen", False)

    def _toggle_fullscreen(self, _event: tk.Event | None = None) -> None:
        current = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not current)


def _status_row(parent: ttk.Widget, row: int, label: str, variable: tk.StringVar) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
    ttk.Label(parent, textvariable=variable, wraplength=340).grid(row=row, column=1, sticky="nw", pady=2)
