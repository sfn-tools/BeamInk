from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import select
import threading
import time
from typing import Callable

from beamink.core.mapping import clamp01
from beamink.core.types import PenSample

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:  # pragma: no cover
    InputDevice = None
    ecodes = None
    list_devices = None


@dataclass(frozen=True, slots=True)
class WacomDeviceInfo:
    path: str
    name: str
    has_pen_axes: bool
    phys: str = ""
    uniq: str = ""


def discover_wacom_devices() -> list[WacomDeviceInfo]:
    if list_devices is None or InputDevice is None or ecodes is None:
        return []

    devices: list[WacomDeviceInfo] = []
    for path in list_devices():
        device = InputDevice(path)
        try:
            name = device.name or ""
            if "Wacom" not in name and "Intuos" not in name:
                continue

            caps = device.capabilities(absinfo=True)
            abs_caps = caps.get(ecodes.EV_ABS, [])
            codes = {code for code, _ in abs_caps}
            has_pen_axes = ecodes.ABS_X in codes and ecodes.ABS_Y in codes and ecodes.ABS_PRESSURE in codes
            devices.append(
                WacomDeviceInfo(
                    path=path,
                    name=name,
                    has_pen_axes=has_pen_axes,
                    phys=device.phys or "",
                    uniq=device.uniq or "",
                )
            )
        finally:
            device.close()

    return devices


def discover_pen_device(preferred_path: str | None = None) -> WacomDeviceInfo:
    devices = discover_wacom_devices()
    if preferred_path:
        for device in devices:
            if device.path == preferred_path:
                return device
        for device in list_sysfs_wacom_devices():
            if device.path == preferred_path:
                return device
        raise RuntimeError(f"Requested Wacom device was not found: {preferred_path}")

    for device in devices:
        if device.has_pen_axes:
            return device

    for device in list_sysfs_wacom_devices():
        if device.has_pen_axes:
            return device
    raise RuntimeError("No Wacom pen device with X/Y/pressure axes was found")


class WacomPenReader:
    def __init__(
        self,
        device_info: WacomDeviceInfo,
        on_sample: Callable[[PenSample], None],
        on_error: Callable[[str], None] | None = None,
        on_capture_state: Callable[[str], None] | None = None,
        exclusive_grab: bool = True,
    ) -> None:
        if InputDevice is None or ecodes is None:
            raise RuntimeError("python-evdev is not installed")
        self.device_info = device_info
        self.on_sample = on_sample
        self.on_error = on_error
        self.on_capture_state = on_capture_state
        self.exclusive_grab = exclusive_grab
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._device: InputDevice | None = None
        self._capture_devices: list[InputDevice] = []
        self._grabbed_devices: list[InputDevice] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wacom-pen-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        for device in self._capture_devices:
            if device in self._grabbed_devices:
                try:
                    device.ungrab()
                except OSError:
                    pass
        self._grabbed_devices = []
        for device in self._capture_devices:
            try:
                device.close()
            except OSError:
                pass
        self._capture_devices = []
        self._device = None

    def _run(self) -> None:
        try:
            device = InputDevice(self.device_info.path)
            self._device = device
            self._capture_devices = [device]
            self._extend_capture_group()
            self._set_capture_state("Shared")
            self._apply_exclusive_grab()

            caps = device.capabilities(absinfo=True)
            abs_caps = {code: info for code, info in caps.get(ecodes.EV_ABS, [])}
            x_info = abs_caps.get(ecodes.ABS_X)
            y_info = abs_caps.get(ecodes.ABS_Y)
            p_info = abs_caps.get(ecodes.ABS_PRESSURE)
            if x_info is None or y_info is None or p_info is None:
                raise RuntimeError(f"Device {self.device_info.path} is missing pen axes")

            x_raw = x_info.min
            y_raw = y_info.min
            p_raw = p_info.min
            touch_state = False
            tool_state = False
            last_motion_ts = 0.0
            dirty = True

            while not self._stop.is_set():
                ready, _, _ = select.select([device.fd], [], [], 0.2)
                if not ready:
                    continue

                for event in device.read():
                    if event.type == ecodes.EV_ABS:
                        if event.code == ecodes.ABS_X:
                            x_raw = event.value
                            last_motion_ts = time.monotonic()
                            dirty = True
                        elif event.code == ecodes.ABS_Y:
                            y_raw = event.value
                            last_motion_ts = time.monotonic()
                            dirty = True
                        elif event.code == ecodes.ABS_PRESSURE:
                            p_raw = event.value
                            last_motion_ts = time.monotonic()
                            dirty = True
                    elif event.type == ecodes.EV_KEY:
                        if event.code == ecodes.BTN_TOUCH:
                            touch_state = bool(event.value)
                            dirty = True
                        elif event.code in (ecodes.BTN_TOOL_PEN, getattr(ecodes, "BTN_TOOL_RUBBER", -1)):
                            tool_state = bool(event.value)
                            dirty = True
                    elif event.type == ecodes.EV_SYN and dirty:
                        now = time.monotonic()
                        pressure_norm = _normalize_axis(p_raw, p_info.min, p_info.max)
                        contact, in_range = derive_pen_state(
                            pressure_norm=pressure_norm,
                            touch_state=touch_state,
                            tool_state=tool_state,
                            last_motion_age_s=(now - last_motion_ts) if last_motion_ts else 999.0,
                        )
                        sample = PenSample(
                            x_norm=_normalize_axis(x_raw, x_info.min, x_info.max),
                            y_norm=_normalize_axis(y_raw, y_info.min, y_info.max),
                            pressure_norm=pressure_norm,
                            contact=contact,
                            in_range=in_range,
                            timestamp=now,
                            device_name=self.device_info.name,
                        )
                        self.on_sample(sample)
                        dirty = False
        except Exception as exc:
            self._report_error(str(exc))

    def _report_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)
        else:  # pragma: no cover
            raise RuntimeError(message)

    def _set_capture_state(self, state: str) -> None:
        if self.on_capture_state is not None:
            self.on_capture_state(state)
    def _extend_capture_group(self) -> None:
        for info in discover_related_wacom_devices(self.device_info):
            if info.path == self.device_info.path:
                continue
            try:
                self._capture_devices.append(InputDevice(info.path))
            except OSError as exc:
                self._report_error(f"failed to open related Wacom device {info.path}: {exc}")

    def _apply_exclusive_grab(self) -> None:
        if not self.exclusive_grab:
            return

        for device in self._capture_devices:
            try:
                device.grab()
                self._grabbed_devices.append(device)
            except OSError as exc:
                self._report_error(f"exclusive grab failed for {device.path}: {exc}")

        total = len(self._capture_devices)
        grabbed = len(self._grabbed_devices)
        if grabbed == total and total > 0:
            self._set_capture_state(f"Exclusive ({grabbed} devices)")
        elif grabbed > 0:
            self._set_capture_state(f"Partial exclusive ({grabbed}/{total})")
        else:
            self._set_capture_state("Shared (grab failed)")


def discover_related_wacom_devices(primary: WacomDeviceInfo) -> list[WacomDeviceInfo]:
    related: list[WacomDeviceInfo] = []
    for device in list_sysfs_wacom_devices():
        same_uniq = bool(primary.uniq and device.uniq and primary.uniq == device.uniq)
        same_phys = bool(primary.phys and device.phys and primary.phys == device.phys)
        same_name_group = "Wacom" in device.name or "Intuos" in device.name
        if same_name_group and (same_uniq or same_phys or device.path == primary.path):
            related.append(device)
    if not related:
        return [primary]
    return related


def list_sysfs_wacom_devices() -> list[WacomDeviceInfo]:
    result: list[WacomDeviceInfo] = []
    for path in sorted(Path("/sys/class/input").glob("event*/device/name")):
        try:
            name = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if "Wacom" in name or "Intuos" in name:
            event_name = path.parents[1].name
            device_root = path.parent
            result.append(
                WacomDeviceInfo(
                    path=f"/dev/input/{event_name}",
                    name=name,
                    has_pen_axes="Pen" in name,
                    phys=_read_sysfs_text(device_root / "phys"),
                    uniq=_read_sysfs_text(device_root / "uniq"),
                )
            )
    return result


def _normalize_axis(value: int, minimum: int, maximum: int) -> float:
    if maximum <= minimum:
        return 0.0
    return clamp01((value - minimum) / float(maximum - minimum))


def derive_pen_state(
    *,
    pressure_norm: float,
    touch_state: bool,
    tool_state: bool,
    last_motion_age_s: float,
    motion_horizon_s: float = 0.25,
) -> tuple[bool, bool]:
    contact = touch_state or pressure_norm > 0.0
    in_range = tool_state or contact or last_motion_age_s <= motion_horizon_s
    return contact, in_range


def _read_sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
