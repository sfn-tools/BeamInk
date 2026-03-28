from __future__ import annotations

from beamink.config import PressureConfig, StageConfig, TabletConfig


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def clamp01(value: float) -> float:
    return clamp(value, 0.0, 1.0)


def map_tablet_to_stage(
    x_norm: float,
    y_norm: float,
    tablet: TabletConfig,
    stage: StageConfig,
) -> tuple[float, float]:
    x_norm = clamp01(x_norm)
    y_norm = clamp01(y_norm)

    inner_width = max(stage.width_mm - 2.0 * stage.safe_margin_mm, 1.0)
    inner_height = max(stage.height_mm - 2.0 * stage.safe_margin_mm, 1.0)

    fit_mode = stage.fit_mode.lower()
    scale_x = inner_width / tablet.active_width_mm
    scale_y = inner_height / tablet.active_height_mm

    if fit_mode == "stretch":
        mapped_x = stage.safe_margin_mm + x_norm * inner_width
        mapped_y = stage.safe_margin_mm + y_norm * inner_height
        return mapped_x, mapped_y

    uniform_scale = min(scale_x, scale_y) if fit_mode == "contain" else max(scale_x, scale_y)
    fitted_width = tablet.active_width_mm * uniform_scale
    fitted_height = tablet.active_height_mm * uniform_scale
    offset_x = stage.safe_margin_mm + (inner_width - fitted_width) / 2.0
    offset_y = stage.safe_margin_mm + (inner_height - fitted_height) / 2.0

    mapped_x = offset_x + x_norm * fitted_width
    mapped_y = offset_y + y_norm * fitted_height
    return clamp(mapped_x, 0.0, stage.width_mm), clamp(mapped_y, 0.0, stage.height_mm)


def pressure_to_power(pressure_norm: float, contact: bool, config: PressureConfig) -> float:
    if not contact:
        if config.pen_up_mode == "preview":
            return clamp(config.hover_power_pct, 0.0, 100.0)
        return 0.0

    floor = clamp01(config.pressure_floor_ratio)
    ceil = clamp01(config.pressure_ceil_ratio)
    if ceil <= floor:
        normalized = 1.0
    else:
        normalized = clamp((clamp01(pressure_norm) - floor) / (ceil - floor), 0.0, 1.0)

    power_span = max(config.contact_power_ceil_pct - config.contact_power_floor_pct, 0.0)
    return clamp(
        config.contact_power_floor_pct + normalized * power_span,
        0.0,
        100.0,
    )


def power_to_xtool_s_value(power_pct: float) -> int:
    return max(0, min(1000, round(clamp(power_pct, 0.0, 100.0) * 10.0)))

