from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
import time
from typing import Any

from beamink.config import TransportConfig
from beamink.core.mapping import power_to_xtool_s_value
from beamink.core.types import LaserCommand, TransportSnapshot


class LaserTransport(ABC):
    def __init__(self, config: TransportConfig, log_limit: int = 200) -> None:
        self.config = config
        self.snapshot = TransportSnapshot(
            name=self.__class__.__name__,
            recent_gcode=deque(maxlen=log_limit),
            recent_replies=deque(maxlen=log_limit),
        )

    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_command(self, command: LaserCommand) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def send_lines(self, lines: list[str]) -> list[str]:
        raise NotImplementedError

    def send_commands(self, commands: list[LaserCommand]) -> list[str]:
        sent: list[str] = []
        for command in commands:
            sent.extend(self.send_command(command))
        return sent

    def query_machine_info(self) -> dict[str, Any]:
        return {}

    def move_to_safe_position(
        self,
        x_mm: float,
        y_mm: float,
        *,
        disconnect_after: bool = False,
    ) -> list[str]:
        lines = self.send_command(
            LaserCommand(
                x_mm=x_mm,
                y_mm=y_mm,
                power_pct=0.0,
                contact=False,
                preview_mode=False,
                timestamp=time.monotonic(),
            )
        )
        if disconnect_after:
            self.disconnect()
        return lines

    def stop_processing(self, reason: str = "") -> dict[str, Any]:
        self.disconnect()
        return {"reason": reason, "transport": self.snapshot.name}

    def get_snapshot(self) -> TransportSnapshot:
        return self.snapshot

    def record_gcode(self, lines: list[str]) -> None:
        for line in lines:
            self.snapshot.recent_gcode.append(line)

    def record_reply(self, message: str) -> None:
        if message:
            self.snapshot.recent_replies.append(message)

    def record_error(self, message: str) -> None:
        self.snapshot.last_error = message
        self.record_reply(f"ERROR: {message}")


def render_xtool_lines(command: LaserCommand, config: TransportConfig) -> list[str]:
    if command.power_pct <= 0.0 and not command.preview_mode:
        return [f"G0X{command.x_mm:.3f}Y{command.y_mm:.3f}", "G0 S0"]

    return [
        (
            f"G1X{command.x_mm:.3f}Y{command.y_mm:.3f}"
            f"S{power_to_xtool_s_value(command.power_pct)}F{config.feed_rate}"
        )
    ]


def laser_source_command(source: str) -> str:
    normalized = source.strip().upper()
    if normalized == "RED":
        return "M114 S2"
    return "M114 S1"


def laser_power_mode_command(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized == "dynamic":
        return "M4 S0"
    return "M3 S0"


def xtool_session_start_lines(config: TransportConfig) -> list[str]:
    return [
        "$L",
        "G0 F180000",
        laser_power_mode_command(config.laser_power_mode),
        "G1 F180000",
        laser_source_command(config.source),
        "G21",
        "G90",
    ]


def xtool_session_end_lines() -> list[str]:
    return [
        "G90",
        "G0 S0",
        "G0 F180000",
        "G1 F180000",
        "M6 P1",
        "$P",
    ]


def render_xtool_centering_lines(*, x_mm: float, y_mm: float) -> list[str]:
    return [
        "G0 S0",
        f"G0X{x_mm:.3f}Y{y_mm:.3f}",
        "G0 S0",
    ]


def render_xtool_motion_smoke_lines(
    *,
    stage_width_mm: float,
    stage_height_mm: float,
    side_mm: float,
    center_x_mm: float | None = None,
    center_y_mm: float | None = None,
    safety_margin_mm: float = 10.0,
) -> list[str]:
    usable_width = stage_width_mm - 2.0 * safety_margin_mm
    usable_height = stage_height_mm - 2.0 * safety_margin_mm
    effective_side = min(side_mm, usable_width, usable_height)
    if effective_side <= 0.0:
        raise ValueError("Requested motion smoke square does not fit inside the stage safety margin")

    half_side = effective_side / 2.0
    if center_x_mm is None:
        center_x_mm = stage_width_mm / 2.0
    if center_y_mm is None:
        center_y_mm = stage_height_mm / 2.0

    min_x = safety_margin_mm + half_side
    max_x = stage_width_mm - safety_margin_mm - half_side
    min_y = safety_margin_mm + half_side
    max_y = stage_height_mm - safety_margin_mm - half_side
    center_x_mm = max(min_x, min(max_x, center_x_mm))
    center_y_mm = max(min_y, min(max_y, center_y_mm))

    left = center_x_mm - half_side
    right = center_x_mm + half_side
    top = center_y_mm - half_side
    bottom = center_y_mm + half_side

    return [
        *render_xtool_centering_lines(x_mm=center_x_mm, y_mm=center_y_mm)[:2],
        f"G0X{left:.3f}Y{top:.3f}",
        f"G0X{right:.3f}Y{top:.3f}",
        f"G0X{right:.3f}Y{bottom:.3f}",
        f"G0X{left:.3f}Y{bottom:.3f}",
        f"G0X{left:.3f}Y{top:.3f}",
        f"G0X{center_x_mm:.3f}Y{center_y_mm:.3f}",
        "G0 S0",
    ]
