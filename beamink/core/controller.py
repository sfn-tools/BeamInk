from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import queue
import threading
import time

from beamink.config import AppConfig
from beamink.core.mapping import map_tablet_to_stage, power_to_xtool_s_value, pressure_to_power
from beamink.core.types import ControllerSnapshot, LaserCommand, PathPoint, PenSample
from beamink.transport.base import LaserTransport


class LaserController:
    def __init__(self, config: AppConfig, transport: LaserTransport) -> None:
        self.config = config
        self.transport = transport
        self.snapshot = ControllerSnapshot(transport_name=transport.get_snapshot().name)

        self._armed = False
        self._lock = threading.Lock()
        self._transport_lock = threading.Lock()
        self._sample_queue: queue.Queue[PenSample] = queue.Queue()
        self._latest_sample: PenSample | None = None
        self._latest_command: LaserCommand | None = None
        self._last_sent_command: LaserCommand | None = None
        self._last_sent_signature: tuple[float, float, float, bool] | None = None
        self._last_motion_timestamp: float | None = None
        self._path_points: deque[PathPoint] = deque(maxlen=config.controller.path_history_limit)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._shutdown_complete = False

        center_x, center_y = self._stage_focus_center()
        self.snapshot.stage_x_mm = center_x
        self.snapshot.stage_y_mm = center_y

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="laser-controller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.stop_processing("Application closing")

    def submit_sample(self, sample: PenSample) -> None:
        self._sample_queue.put(sample)

    def set_device_info(self, *, name: str, path: str) -> None:
        with self._lock:
            self.snapshot.device_name = name
            self.snapshot.device_path = path

    def set_input_capture_state(self, state: str) -> None:
        with self._lock:
            self.snapshot.input_capture_text = state

    def set_armed(self, armed: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._armed = armed
            self.snapshot.armed = armed
            if armed:
                self._last_motion_timestamp = now
            if not armed:
                self._last_motion_timestamp = None
                self.snapshot.status_text = "Output disarmed"
        if not armed:
            self._send_safe_shutdown()

    def clear_path(self) -> None:
        with self._lock:
            self._path_points.clear()
            self.snapshot.path_points = []

    def report_error(self, message: str) -> None:
        with self._lock:
            self.snapshot.last_error = message

    def update_pressure_config(
        self,
        *,
        hover_power_pct: float,
        contact_power_floor_pct: float,
        contact_power_ceil_pct: float,
        pen_up_mode: str | None = None,
    ) -> None:
        pressure = self.config.pressure
        pressure.hover_power_pct = hover_power_pct
        pressure.contact_power_floor_pct = contact_power_floor_pct
        pressure.contact_power_ceil_pct = contact_power_ceil_pct
        if pen_up_mode is not None:
            pressure.pen_up_mode = pen_up_mode

    def update_transport_config(self, *, feed_rate: int | None = None) -> None:
        if feed_rate is not None:
            self.config.transport.feed_rate = max(1, int(feed_rate))

    def get_snapshot(self) -> ControllerSnapshot:
        with self._lock:
            return deepcopy(self.snapshot)

    def refresh_machine_info(self) -> dict[str, object]:
        with self._transport_lock:
            info = self.transport.query_machine_info()
        with self._lock:
            self.snapshot.machine_info = deepcopy(info)
            if isinstance(info, dict):
                stage = info.get("data", {}).get("workSize")
                if isinstance(stage, dict):
                    width = stage.get("x")
                    height = stage.get("y")
                    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
                        self.config.stage.width_mm = float(width)
                        self.config.stage.height_mm = float(height)
                        focus_x, focus_y = self._stage_focus_center()
                        self.snapshot.stage_x_mm = focus_x
                        self.snapshot.stage_y_mm = focus_y
            return deepcopy(self.snapshot.machine_info)

    def prepare_machine(self) -> None:
        try:
            self.refresh_machine_info()
        except Exception as exc:
            self.report_error(f"startup machine info failed: {exc}")
        self.move_to_stage_center("Startup centering", disconnect_after=True)

    def process_cycle(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        samples = self._drain_samples()
        if samples:
            for sample in samples:
                self._record_motion(sample)
                self._latest_sample = sample
                self._record_path_point(sample)

        command = self._build_command(now)
        self._latest_command = command

        with self._lock:
            armed = self._armed
            self.snapshot.stage_x_mm = command.x_mm
            self.snapshot.stage_y_mm = command.y_mm
            self.snapshot.contact = bool(self._latest_sample and self._latest_sample.contact)
            self.snapshot.in_range = bool(self._latest_sample and self._latest_sample.in_range)
            self.snapshot.intended_power_pct = command.power_pct
            self.snapshot.output_power_pct = command.power_pct if armed else 0.0
            self.snapshot.path_points = list(self._path_points)
            self.snapshot.status_text = self._build_status_text(armed, now)

        if self._should_stop_for_idle(now, armed):
            self.stop_processing(
                f"Idle stop after {self.config.controller.idle_processing_stop_s:.0f}s without pen motion"
            )
            return

        if armed:
            pending_commands = self._collect_pending_commands(samples)
            if pending_commands:
                try:
                    with self._transport_lock:
                        self.transport.send_commands(pending_commands)
                    last_command = pending_commands[-1]
                    self._last_sent_signature = self._command_signature(last_command)
                    self._last_sent_command = last_command
                except OSError:
                    with self._lock:
                        self.snapshot.last_error = self.transport.get_snapshot().last_error
            else:
                signature = self._command_signature(command)
                if signature != self._last_sent_signature and self._should_emit_command(command):
                    try:
                        with self._transport_lock:
                            self.transport.send_command(command)
                        self._last_sent_signature = signature
                        self._last_sent_command = command
                    except OSError:
                        with self._lock:
                            self.snapshot.last_error = self.transport.get_snapshot().last_error

        self._sync_transport_snapshot()

    def _run(self) -> None:
        period = 1.0 / max(self.config.transport.send_rate_hz, 1.0)
        while not self._stop.is_set():
            self.process_cycle()
            self._stop.wait(period)

    def _drain_samples(self) -> list[PenSample]:
        samples: list[PenSample] = []
        while True:
            try:
                samples.append(self._sample_queue.get_nowait())
            except queue.Empty:
                return samples

    def _record_path_point(self, sample: PenSample) -> None:
        if not sample.in_range:
            return
        x_mm, y_mm = map_tablet_to_stage(sample.x_norm, sample.y_norm, self.config.tablet, self.config.stage)
        self._path_points.append(PathPoint(x_mm=x_mm, y_mm=y_mm, contact=sample.contact))

    def _record_motion(self, sample: PenSample) -> None:
        previous = self._latest_sample
        if previous is None:
            self._last_motion_timestamp = sample.timestamp
            return
        if not sample.in_range:
            return
        if not previous.in_range:
            self._last_motion_timestamp = sample.timestamp
            return

        epsilon = self.config.controller.idle_motion_epsilon_norm
        if (
            abs(sample.x_norm - previous.x_norm) >= epsilon
            or abs(sample.y_norm - previous.y_norm) >= epsilon
        ):
            self._last_motion_timestamp = sample.timestamp

    def _build_command(self, now: float) -> LaserCommand:
        sample = self._latest_sample
        if sample is None:
            center_x, center_y = self._stage_focus_center()
            return LaserCommand(center_x, center_y, 0.0, False, False, now)

        if (now - sample.timestamp) > self.config.controller.stale_input_timeout_s:
            x_mm, y_mm = map_tablet_to_stage(sample.x_norm, sample.y_norm, self.config.tablet, self.config.stage)
            return LaserCommand(x_mm, y_mm, 0.0, False, False, now)

        return self._build_command_from_sample(sample, timestamp=now)

    def _build_status_text(self, armed: bool, now: float) -> str:
        sample = self._latest_sample
        if sample is None:
            return "Waiting for Wacom pen input"
        if now - sample.timestamp > self.config.controller.stale_input_timeout_s:
            return "Input stale, forcing safe state"
        if not sample.in_range:
            return "Pen out of range"
        if sample.contact:
            return "Streaming draw intent" if armed else "Tracking pen contact (disarmed)"
        return "Preview motion" if armed else "Tracking hover (disarmed)"

    def _sync_transport_snapshot(self) -> None:
        transport_snapshot = self.transport.get_snapshot()
        with self._lock:
            self.snapshot.transport_name = transport_snapshot.name
            self.snapshot.transport_connected = transport_snapshot.connected
            if transport_snapshot.last_error:
                self.snapshot.last_error = transport_snapshot.last_error
            self.snapshot.recent_gcode = list(transport_snapshot.recent_gcode)
            self.snapshot.recent_replies = list(transport_snapshot.recent_replies)
            self.snapshot.machine_info = deepcopy(transport_snapshot.machine_info)

    def _command_signature(self, command: LaserCommand) -> tuple[float, float, float, bool]:
        return (
            round(command.x_mm, 3),
            round(command.y_mm, 3),
            round(command.power_pct, 2),
            command.preview_mode,
        )

    def _should_stop_for_idle(self, now: float, armed: bool) -> bool:
        timeout_s = self.config.controller.idle_processing_stop_s
        if timeout_s <= 0.0:
            return False
        if not armed and not self.transport.get_snapshot().connected:
            return False
        if self._last_motion_timestamp is None:
            return False
        return (now - self._last_motion_timestamp) >= timeout_s

    def _build_command_from_sample(self, sample: PenSample, *, timestamp: float) -> LaserCommand:
        x_mm, y_mm = map_tablet_to_stage(sample.x_norm, sample.y_norm, self.config.tablet, self.config.stage)
        if not sample.in_range:
            return LaserCommand(x_mm, y_mm, 0.0, False, False, timestamp)

        power_pct = pressure_to_power(sample.pressure_norm, sample.contact, self.config.pressure)
        preview_mode = (not sample.contact) and self.config.pressure.pen_up_mode == "preview" and power_pct > 0.0
        return LaserCommand(
            x_mm=x_mm,
            y_mm=y_mm,
            power_pct=power_pct,
            contact=sample.contact,
            preview_mode=preview_mode,
            timestamp=timestamp,
        )

    def _collect_pending_commands(self, samples: list[PenSample]) -> list[LaserCommand]:
        pending: list[LaserCommand] = []
        for sample in samples:
            command = self._build_command_from_sample(sample, timestamp=sample.timestamp)
            baseline = pending[-1] if pending else self._last_sent_command
            if baseline is None or self._command_changed_enough(baseline, command):
                pending.append(command)
        return pending

    def _should_emit_command(self, command: LaserCommand) -> bool:
        if self._last_sent_command is None:
            return True
        return self._command_changed_enough(self._last_sent_command, command)

    def _command_changed_enough(self, previous: LaserCommand, current: LaserCommand) -> bool:
        previous_s = power_to_xtool_s_value(previous.power_pct)
        current_s = power_to_xtool_s_value(current.power_pct)
        power_delta_s = abs(current_s - previous_s)
        movement_mm = math.hypot(current.x_mm - previous.x_mm, current.y_mm - previous.y_mm)
        min_step_mm = self.config.controller.min_motion_step_mm
        mode_changed = previous.preview_mode != current.preview_mode or previous.contact != current.contact
        min_stationary_delta_s = max(
            1,
            round(self.config.controller.min_stationary_power_delta_pct * 10.0),
        )

        if current_s == 0 and previous_s > 0:
            return True
        if movement_mm >= min_step_mm:
            return True
        if mode_changed and current_s == 0:
            return True
        if self.config.transport.laser_power_mode.strip().lower() != "constant":
            return False
        if mode_changed and (current_s > 0 or previous_s > 0):
            return True
        if power_delta_s >= min_stationary_delta_s and (current_s > 0 or previous_s > 0):
            return True
        return False

    def move_to_stage_center(self, reason: str, *, disconnect_after: bool, update_status: bool = True) -> None:
        center_x, center_y = self._stage_focus_center()
        try:
            with self._transport_lock:
                self.transport.move_to_safe_position(center_x, center_y, disconnect_after=disconnect_after)
        except Exception as exc:
            with self._lock:
                self.snapshot.last_error = str(exc)
            return

        with self._lock:
            self._latest_command = LaserCommand(
                x_mm=center_x,
                y_mm=center_y,
                power_pct=0.0,
                contact=False,
                preview_mode=False,
                timestamp=time.monotonic(),
            )
            self.snapshot.stage_x_mm = center_x
            self.snapshot.stage_y_mm = center_y
            self.snapshot.output_power_pct = 0.0
            if update_status:
                self.snapshot.status_text = reason
        self._last_sent_signature = None
        self._last_sent_command = None
        self._sync_transport_snapshot()

    def stop_processing(self, reason: str) -> None:
        with self._lock:
            self._armed = False
            self._last_motion_timestamp = None
            self.snapshot.armed = False
            self.snapshot.output_power_pct = 0.0
            self.snapshot.status_text = reason
            self.snapshot.last_error = ""

        self.move_to_stage_center(f"{reason} (centering)", disconnect_after=False, update_status=False)

        try:
            with self._transport_lock:
                self.transport.stop_processing(reason=reason)
        except Exception as exc:
            with self._lock:
                self.snapshot.last_error = str(exc)
        finally:
            self._last_sent_signature = None
            self._last_sent_command = None
            self._sync_transport_snapshot()

    def _stage_focus_center(self) -> tuple[float, float]:
        return (
            max(0.0, min(self.config.stage.focus_center_x_mm, self.config.stage.width_mm)),
            max(0.0, min(self.config.stage.focus_center_y_mm, self.config.stage.height_mm)),
        )

    def _send_safe_shutdown(self) -> None:
        if self._latest_command is None:
            with self._transport_lock:
                self.transport.disconnect()
            return

        safe_command = LaserCommand(
            x_mm=self._latest_command.x_mm,
            y_mm=self._latest_command.y_mm,
            power_pct=0.0,
            contact=False,
            preview_mode=False,
            timestamp=time.monotonic(),
        )
        try:
            with self._transport_lock:
                if self.transport.get_snapshot().connected:
                    self.transport.send_command(safe_command)
        except OSError:
            pass
        finally:
            with self._transport_lock:
                self.transport.disconnect()
            self._last_sent_signature = None
            self._last_sent_command = None
