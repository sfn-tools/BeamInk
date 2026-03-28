from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from beamink.config import AppConfig, dump_default_config, load_config
from beamink.core.controller import LaserController
from beamink.input.wacom import WacomPenReader, discover_pen_device, discover_wacom_devices, list_sysfs_wacom_devices
from beamink.transport.base import render_xtool_motion_smoke_lines, xtool_session_end_lines, xtool_session_start_lines
from beamink.transport.sim import SimulatorTransport
from beamink.transport.xtool import XToolTransport


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.dump_default_config:
        print(dump_default_config())
        return 0

    if args.list_devices:
        return _list_devices()

    config = load_config(args.config)
    _apply_cli_overrides(config, args)

    transport = build_transport(config)
    controller = LaserController(config, transport)

    if args.machine_info:
        info = controller.refresh_machine_info()
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0

    if args.preview_motion_smoke:
        summary = run_preview_motion_smoke(controller, transport, config, args)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if config.transport.mode.lower() == "xtool":
        controller.prepare_machine()

    reader = None
    try:
        device_info = discover_pen_device(config.tablet.device_path)
        controller.set_device_info(name=device_info.name, path=device_info.path)
        reader = WacomPenReader(
            device_info=device_info,
            on_sample=controller.submit_sample,
            on_error=controller.report_error,
            on_capture_state=controller.set_input_capture_state,
            exclusive_grab=config.tablet.exclusive_grab,
        )
        reader.start()
    except Exception as exc:
        controller.report_error(str(exc))
        print(f"[BeamInk] input warning: {exc}", file=sys.stderr)

    controller.start()

    def shutdown() -> None:
        if reader is not None:
            reader.stop()
        controller.stop()

    def handle_signal(_signum, _frame) -> None:
        shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if args.no_gui:
            return _run_console(controller)

        import tkinter as tk

        from beamink.gui.main_window import MainWindow

        root = tk.Tk()
        MainWindow(root, controller, config)
        root.mainloop()
        return 0
    finally:
        shutdown()


def build_transport(config: AppConfig):
    if config.transport.mode.lower() == "xtool":
        return XToolTransport(config.transport, log_limit=config.controller.debug_log_limit)
    return SimulatorTransport(config.transport, log_limit=config.controller.debug_log_limit)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BeamInk: realtime Wacom-to-xTool controller")
    parser.add_argument("--config", help="Path to a JSON config file")
    parser.add_argument("--transport", choices=["sim", "xtool"], help="Override transport mode")
    parser.add_argument("--device", help="Override Wacom pen device path")
    parser.add_argument("--host", help="Override xTool host")
    parser.add_argument("--source", choices=["BLUE", "RED"], help="Laser source selection")
    parser.add_argument("--no-grab", action="store_true", help="Do not exclusively grab the Wacom input device")
    parser.add_argument("--fullscreen", action="store_true", help="Start the GUI in fullscreen mode")
    parser.add_argument("--windowed", action="store_true", help="Force the GUI to start windowed")
    parser.add_argument(
        "--preview-motion-smoke",
        action="store_true",
        help="Run a bounded xTool motion smoke test that sends only G0/G0 S0 commands and exits",
    )
    parser.add_argument(
        "--motion-smoke-side-mm",
        type=float,
        default=5.0,
        help="Square side length in mm for --preview-motion-smoke",
    )
    parser.add_argument("--motion-smoke-center-x-mm", type=float, help="Optional X center for --preview-motion-smoke")
    parser.add_argument("--motion-smoke-center-y-mm", type=float, help="Optional Y center for --preview-motion-smoke")
    parser.add_argument("--no-gui", action="store_true", help="Run the controller without the desktop UI")
    parser.add_argument("--machine-info", action="store_true", help="Query machine info over HTTP and exit")
    parser.add_argument("--list-devices", action="store_true", help="List discovered Wacom devices and exit")
    parser.add_argument("--dump-default-config", action="store_true", help="Print the default JSON config and exit")
    return parser


def _apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    if args.transport:
        config.transport.mode = args.transport
    if args.device:
        config.tablet.device_path = args.device
    if args.host:
        config.transport.host = args.host
    if args.source:
        config.transport.source = args.source
    if args.no_grab:
        config.tablet.exclusive_grab = False
    if args.fullscreen:
        config.ui.fullscreen_on_start = True
    if args.windowed:
        config.ui.fullscreen_on_start = False


def _list_devices() -> int:
    discovered = discover_wacom_devices()
    if discovered:
        for device in discovered:
            marker = "pen" if device.has_pen_axes else "pad"
            print(f"{device.path}\t{device.name}\t{marker}")
    else:
        for device in list_sysfs_wacom_devices():
            marker = "pen" if device.has_pen_axes else "pad"
            print(f"{device.path}\t{device.name}\t{marker}")
    return 0


def _run_console(controller: LaserController) -> int:
    while True:
        snapshot = controller.get_snapshot()
        print(
            (
                f"{snapshot.status_text} | armed={snapshot.armed} | "
                f"capture={snapshot.input_capture_text} | "
                f"stage=({snapshot.stage_x_mm:.2f}, {snapshot.stage_y_mm:.2f}) | "
                f"power={snapshot.intended_power_pct:.1f}% | "
                f"transport={snapshot.transport_name}:{'up' if snapshot.transport_connected else 'down'}"
            )
        )
        time.sleep(0.5)


def run_preview_motion_smoke(
    controller: LaserController,
    transport,
    config: AppConfig,
    args: argparse.Namespace,
) -> dict[str, object]:
    stage_width = config.stage.width_mm
    stage_height = config.stage.height_mm
    machine_info: dict[str, object] = {}

    try:
        machine_info = controller.refresh_machine_info()
        if isinstance(machine_info, dict):
            work_size = machine_info.get("data", {}).get("workSize")
            if isinstance(work_size, dict):
                width = work_size.get("x")
                height = work_size.get("y")
                if isinstance(width, (int, float)) and isinstance(height, (int, float)):
                    stage_width = float(width)
                    stage_height = float(height)
    except Exception as exc:
        machine_info = {"warning": f"machine-info query failed: {exc}"}

    smoke_lines = render_xtool_motion_smoke_lines(
        stage_width_mm=stage_width,
        stage_height_mm=stage_height,
        side_mm=args.motion_smoke_side_mm,
        center_x_mm=(
            args.motion_smoke_center_x_mm
            if args.motion_smoke_center_x_mm is not None
            else config.stage.focus_center_x_mm
        ),
        center_y_mm=(
            args.motion_smoke_center_y_mm
            if args.motion_smoke_center_y_mm is not None
            else config.stage.focus_center_y_mm
        ),
    )
    lines = xtool_session_start_lines(config.transport) + smoke_lines + xtool_session_end_lines()

    try:
        sent_lines = transport.send_lines(lines)
    finally:
        transport.disconnect()

    snapshot = transport.get_snapshot()
    return {
        "machine_info": machine_info,
        "mode": config.transport.mode,
        "host": config.transport.host,
        "tcp_port": config.transport.tcp_port,
        "stage_width_mm": stage_width,
        "stage_height_mm": stage_height,
        "motion_smoke_side_mm": args.motion_smoke_side_mm,
        "capture": {
            "connected": snapshot.connected,
            "last_error": snapshot.last_error,
            "recent_replies": list(snapshot.recent_replies),
        },
        "sent_lines": sent_lines,
    }
