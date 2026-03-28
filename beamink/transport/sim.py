from __future__ import annotations

from beamink.core.types import LaserCommand
from beamink.transport.base import LaserTransport, laser_source_command, render_xtool_lines


class SimulatorTransport(LaserTransport):
    def connect(self) -> None:
        self.snapshot.connected = True
        self.record_reply("sim transport connected")

    def disconnect(self) -> None:
        if self.snapshot.connected:
            self.record_reply("sim transport disconnected")
        self.snapshot.connected = False

    def send_command(self, command: LaserCommand) -> list[str]:
        if not self.snapshot.connected:
            self.connect()

        lines = render_xtool_lines(command, self.config)
        self.record_gcode(lines)
        return [laser_source_command(self.config.source)] + lines

    def send_commands(self, commands: list[LaserCommand]) -> list[str]:
        if not self.snapshot.connected:
            self.connect()

        lines: list[str] = []
        for command in commands:
            lines.extend(render_xtool_lines(command, self.config))
        self.record_gcode(lines)
        return [laser_source_command(self.config.source)] + lines

    def send_lines(self, lines: list[str]) -> list[str]:
        if not self.snapshot.connected:
            self.connect()
        self.record_gcode(lines)
        return lines

    def query_machine_info(self) -> dict[str, object]:
        info = {
            "source": self.config.source,
            "host": self.config.host,
            "tcp_port": self.config.tcp_port,
            "mode": "sim",
        }
        self.snapshot.machine_info = info
        return info

    def stop_processing(self, reason: str = "") -> dict[str, object]:
        if reason:
            self.record_reply(f"sim processing stop: {reason}")
        else:
            self.record_reply("sim processing stop")
        self.disconnect()
        return {"reason": reason, "transport": "sim"}
