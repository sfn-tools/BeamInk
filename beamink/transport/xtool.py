from __future__ import annotations

import http.client
import json
import select
import socket
from typing import Any

from beamink.core.types import LaserCommand
from beamink.transport.base import (
    LaserTransport,
    laser_source_command,
    render_xtool_centering_lines,
    render_xtool_lines,
    xtool_session_end_lines,
    xtool_session_start_lines,
)


class XToolTransport(LaserTransport):
    def __init__(self, config, log_limit: int = 200) -> None:
        super().__init__(config, log_limit=log_limit)
        self._socket: socket.socket | None = None
        self._source_initialized = False

    def connect(self) -> None:
        if self._socket is not None:
            return

        try:
            sock = socket.create_connection(
                (self.config.host, self.config.tcp_port),
                timeout=self.config.connect_timeout_s,
            )
        except OSError as exc:
            self.record_error(f"connect failed: {exc}")
            raise

        sock.settimeout(self.config.read_timeout_s)
        self._socket = sock
        self.snapshot.connected = True
        self.record_reply(f"connected to {self.config.host}:{self.config.tcp_port}")

    def disconnect(self) -> None:
        self._close_socket(send_safe_line=True)

    def send_command(self, command: LaserCommand) -> list[str]:
        return self.send_commands([command])

    def send_commands(self, commands: list[LaserCommand]) -> list[str]:
        if not commands:
            return []
        if self._socket is None:
            self.connect()

        setup_lines: list[str] = []
        if not self._source_initialized:
            setup_lines = xtool_session_start_lines(self.config)
            self._source_initialized = True

        motion_lines: list[str] = []
        for command in commands:
            motion_lines.extend(render_xtool_lines(command, self.config))
        lines = setup_lines + motion_lines
        self._send_lines(lines)
        return lines

    def send_lines(self, lines: list[str]) -> list[str]:
        if self._socket is None:
            self.connect()
        self._send_lines(lines)
        return lines

    def query_machine_info(self) -> dict[str, Any]:
        result = self._request_http("GET", "/device/machineInfo")
        payload = result["payload"]
        self.snapshot.machine_info = payload
        return payload

    def move_to_safe_position(
        self,
        x_mm: float,
        y_mm: float,
        *,
        disconnect_after: bool = False,
    ) -> list[str]:
        centering_lines = render_xtool_centering_lines(x_mm=x_mm, y_mm=y_mm)
        if self._socket is not None:
            self._send_lines(centering_lines)
            if disconnect_after:
                self.disconnect()
            return centering_lines

        lines = xtool_session_start_lines(self.config) + centering_lines
        if disconnect_after:
            lines += xtool_session_end_lines()

        self.send_lines(lines)
        if disconnect_after:
            self._close_socket(send_safe_line=False)
        return lines

    def stop_processing(self, reason: str = "") -> dict[str, Any]:
        errors: list[str] = []

        if self._socket is not None:
            try:
                self._send_lines(["G0 S0"])
            except OSError as exc:
                errors.append(f"safe line failed: {exc}")

        result: dict[str, Any] | None = None
        for method in ("POST", "GET"):
            try:
                body = b"" if method == "POST" else None
                result = self._request_http(method, "/processing/stop", body=body)
                self.record_reply(f"{method} /processing/stop -> {result['status']}")
                break
            except Exception as exc:
                errors.append(f"{method} /processing/stop failed: {exc}")

        self._close_socket(send_safe_line=False)

        if result is None:
            message = "processing stop failed"
            if errors:
                message += f": {'; '.join(errors)}"
            self.record_error(message)
            raise RuntimeError(message)

        if reason:
            self.record_reply(f"processing stop reason: {reason}")
        self.snapshot.connected = False
        self._source_initialized = False
        return result

    def _send_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        if self._socket is None:
            raise RuntimeError("transport socket is not connected")

        payload = "\n".join(lines) + "\n"
        try:
            self._socket.sendall(payload.encode("utf-8"))
            self.record_gcode(lines)
            self._read_available()
        except OSError as exc:
            self.record_error(f"send failed: {exc}")
            self.snapshot.connected = False
            raise

    def _read_available(self) -> None:
        if self._socket is None:
            return
        wait_s = max(0.0, min(self.config.read_timeout_s, 0.01))
        first_read = True
        while True:
            try:
                ready, _, _ = select.select([self._socket], [], [], wait_s if first_read else 0.0)
            except OSError as exc:
                self.record_error(f"read readiness failed: {exc}")
                return

            if not ready:
                return
            first_read = False

            try:
                data = self._socket.recv(4096)
            except socket.timeout:
                return
            except OSError as exc:
                self.record_error(f"read failed: {exc}")
                return

            if not data:
                return
            self.record_reply(data.decode("utf-8", errors="replace").strip())

    def _request_http(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        conn = http.client.HTTPConnection(
            self.config.host,
            self.config.http_port,
            timeout=self.config.connect_timeout_s,
        )
        try:
            conn.request(method, path, body=body)
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"{method} {path} returned HTTP {response.status} {response.reason}")

        payload: Any
        text = response_body.decode("utf-8", errors="replace").strip()
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"raw": text}
        else:
            payload = {}

        return {
            "method": method,
            "path": path,
            "status": response.status,
            "reason": response.reason,
            "payload": payload,
        }

    def _close_socket(self, *, send_safe_line: bool) -> None:
        if self._socket is not None:
            if send_safe_line:
                try:
                    self._send_lines(["G0 S0"])
                except OSError:
                    pass
            try:
                self._socket.close()
            finally:
                self._socket = None
        self._source_initialized = False
        self.snapshot.connected = False
