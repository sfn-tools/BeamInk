from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PenSample:
    x_norm: float
    y_norm: float
    pressure_norm: float
    contact: bool
    in_range: bool
    timestamp: float
    device_name: str = ""


@dataclass(frozen=True, slots=True)
class LaserCommand:
    x_mm: float
    y_mm: float
    power_pct: float
    contact: bool
    preview_mode: bool
    timestamp: float


@dataclass(frozen=True, slots=True)
class PathPoint:
    x_mm: float
    y_mm: float
    contact: bool


@dataclass(slots=True)
class TransportSnapshot:
    name: str
    connected: bool = False
    last_error: str = ""
    recent_gcode: deque[str] = field(default_factory=deque)
    recent_replies: deque[str] = field(default_factory=deque)
    machine_info: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ControllerSnapshot:
    armed: bool = False
    device_name: str = ""
    device_path: str = ""
    input_capture_text: str = "Shared"
    status_text: str = "Waiting for input"
    stage_x_mm: float = 0.0
    stage_y_mm: float = 0.0
    intended_power_pct: float = 0.0
    output_power_pct: float = 0.0
    contact: bool = False
    in_range: bool = False
    last_error: str = ""
    path_points: list[PathPoint] = field(default_factory=list)
    transport_name: str = ""
    transport_connected: bool = False
    recent_gcode: list[str] = field(default_factory=list)
    recent_replies: list[str] = field(default_factory=list)
    machine_info: dict[str, Any] = field(default_factory=dict)
