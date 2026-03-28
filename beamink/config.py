from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TabletConfig:
    active_width_mm: float = 152.0
    active_height_mm: float = 95.0
    device_path: str | None = None
    exclusive_grab: bool = True


@dataclass(slots=True)
class StageConfig:
    width_mm: float = 119.98
    height_mm: float = 119.98
    focus_center_x_mm: float = 57.5
    focus_center_y_mm: float = 57.5
    safe_margin_mm: float = 2.0
    fit_mode: str = "contain"


@dataclass(slots=True)
class PressureConfig:
    pen_up_mode: str = "off"
    hover_power_pct: float = 2.0
    pressure_floor_ratio: float = 0.05
    pressure_ceil_ratio: float = 1.0
    contact_power_floor_pct: float = 5.0
    contact_power_ceil_pct: float = 100.0


@dataclass(slots=True)
class TransportConfig:
    mode: str = "sim"
    host: str = "201.234.3.1"
    tcp_port: int = 8780
    http_port: int = 8080
    source: str = "BLUE"
    laser_power_mode: str = "constant"
    feed_rate: int = 9600
    connect_timeout_s: float = 1.0
    read_timeout_s: float = 0.2
    send_rate_hz: float = 120.0


@dataclass(slots=True)
class ControllerConfig:
    stale_input_timeout_s: float = 0.25
    idle_processing_stop_s: float = 30.0
    idle_motion_epsilon_norm: float = 0.002
    min_motion_step_mm: float = 0.05
    min_stationary_power_delta_pct: float = 2.0
    debug_log_limit: int = 200
    path_history_limit: int = 4000


@dataclass(slots=True)
class UIConfig:
    poll_interval_ms: int = 33
    stage_canvas_px: int = 640
    fullscreen_on_start: bool = False
    stay_on_top: bool = True
    hide_cursor: bool = False


@dataclass(slots=True)
class AppConfig:
    tablet: TabletConfig
    stage: StageConfig
    pressure: PressureConfig
    transport: TransportConfig
    controller: ControllerConfig
    ui: UIConfig

    @classmethod
    def defaults(cls) -> "AppConfig":
        return cls(
            tablet=TabletConfig(),
            stage=StageConfig(),
            pressure=PressureConfig(),
            transport=TransportConfig(),
            controller=ControllerConfig(),
            ui=UIConfig(),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        defaults = cls.defaults()
        return cls(
            tablet=_merge_dataclass(TabletConfig, defaults.tablet, data.get("tablet", {})),
            stage=_merge_dataclass(StageConfig, defaults.stage, data.get("stage", {})),
            pressure=_merge_dataclass(PressureConfig, defaults.pressure, data.get("pressure", {})),
            transport=_merge_dataclass(TransportConfig, defaults.transport, data.get("transport", {})),
            controller=_merge_dataclass(ControllerConfig, defaults.controller, data.get("controller", {})),
            ui=_merge_dataclass(UIConfig, defaults.ui, data.get("ui", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tablet": asdict(self.tablet),
            "stage": asdict(self.stage),
            "pressure": asdict(self.pressure),
            "transport": asdict(self.transport),
            "controller": asdict(self.controller),
            "ui": asdict(self.ui),
        }


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig.defaults()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a top-level object: {config_path}")
    return AppConfig.from_dict(raw)


def dump_default_config() -> str:
    return json.dumps(AppConfig.defaults().to_dict(), indent=2, sort_keys=True)


def _merge_dataclass(cls: type[Any], defaults: Any, data: dict[str, Any]) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"Expected object for {cls.__name__}, got: {type(data).__name__}")

    values = asdict(defaults)
    for key, value in data.items():
        if key not in values:
            raise ValueError(f"Unknown field for {cls.__name__}: {key}")
        values[key] = value
    return cls(**values)
