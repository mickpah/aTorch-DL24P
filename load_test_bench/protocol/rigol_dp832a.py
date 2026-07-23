"""Rigol DP832A power supply driver - raw SCPI over LAN (TCP port 5555).

Mirrors the structure of USBHIDDevice in device.py: a daemon thread polls the
instrument every POLL_INTERVAL and pushes ChargerStatus snapshots to a
callback; commands from the GUI acquire the lock with a timeout so a slow
network can never freeze the UI (see CLAUDE.md "Lock Timeout for GUI
Operations"). The status callback runs on the poll thread - GUI consumers
must marshal it through a Qt Signal.
"""

import socket
import threading
import time
from typing import Callable, Optional

from .dp832a_protocol import ChargerStatus, DP832AProtocol


class ChargerError(Exception):
    """Raised on DP832A connection or identification failures."""


class RigolDP832A:
    DEFAULT_PORT = 5555
    POLL_INTERVAL = 1.0  # seconds
    SOCKET_TIMEOUT = 2.0  # seconds
    GUI_LOCK_TIMEOUT = 1.0  # seconds

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._connected = False
        self._host: Optional[str] = None
        self._channel = 1
        self._identity = ""
        self._lock = threading.Lock()
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._last_status: Optional[ChargerStatus] = None
        self._status_callback: Optional[Callable[[ChargerStatus], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def channel(self) -> int:
        return self._channel

    @property
    def last_status(self) -> Optional[ChargerStatus]:
        return self._last_status

    def set_channel(self, channel: int) -> None:
        DP832AProtocol.check_channel(channel)
        self._channel = channel

    def set_status_callback(self, callback: Callable[[ChargerStatus], None]) -> None:
        self._status_callback = callback

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback

    def connect(self, host: str, port: int = DEFAULT_PORT) -> bool:
        if self._connected:
            return True
        try:
            sock = socket.create_connection((host, port), timeout=self.SOCKET_TIMEOUT)
            sock.settimeout(self.SOCKET_TIMEOUT)
        except OSError as e:
            raise ChargerError(f"Cannot reach DP832A at {host}:{port}: {e}") from e
        self._sock = sock
        try:
            idn = self._query(DP832AProtocol.cmd_idn())
        except OSError as e:
            self._close_socket()
            raise ChargerError(f"No SCPI response from {host}:{port}: {e}") from e
        if not DP832AProtocol.parse_idn(idn):
            self._close_socket()
            raise ChargerError(f"Device at {host}:{port} is not a DP832A: {idn!r}")
        self._identity = idn.strip()
        self._host = host
        self._connected = True
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        return True

    def disconnect(self) -> None:
        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=self.SOCKET_TIMEOUT + 1.0)
        self._poll_thread = None
        self._close_socket()
        self._connected = False
        self._last_status = None

    def set_voltage(self, volts: float) -> bool:
        return self._command(DP832AProtocol.cmd_set_voltage(self._channel, volts))

    def set_current(self, amps: float) -> bool:
        return self._command(DP832AProtocol.cmd_set_current(self._channel, amps))

    def set_ovp(self, volts: float) -> bool:
        ok = self._command(DP832AProtocol.cmd_set_ovp_value(self._channel, volts))
        return ok and self._command(DP832AProtocol.cmd_set_ovp_state(self._channel, True))

    def output_on(self) -> bool:
        return self._command(DP832AProtocol.cmd_set_output(self._channel, True))

    def output_off(self) -> bool:
        return self._command(DP832AProtocol.cmd_set_output(self._channel, False))

    def _command(self, cmd: str) -> bool:
        if not self._lock.acquire(timeout=self.GUI_LOCK_TIMEOUT):
            self._report_error(f"Charger busy, command dropped: {cmd}")
            return False
        try:
            self._write(cmd)
            return True
        except OSError as e:
            self._report_error(f"Charger command failed: {e}")
            return False
        finally:
            self._lock.release()

    def _write(self, cmd: str) -> None:
        self._sock.sendall((cmd + "\n").encode("ascii"))

    def _read_line(self) -> str:
        chunks = []
        while True:
            data = self._sock.recv(4096)
            if not data:
                raise OSError("Connection closed by instrument")
            chunks.append(data)
            if data.endswith(b"\n"):
                break
        return b"".join(chunks).decode("ascii").strip()

    def _query(self, cmd: str) -> str:
        self._write(cmd)
        return self._read_line()

    def _poll_once(self) -> ChargerStatus:
        proto = DP832AProtocol
        with self._lock:
            ch = self._channel
            volts, amps, watts = proto.parse_measure_all(
                self._query(proto.cmd_measure_all(ch))
            )
            output_on = proto.parse_output_state(self._query(proto.cmd_query_output(ch)))
            mode = proto.parse_mode(self._query(proto.cmd_query_mode(ch))) if output_on else "UR"
        return ChargerStatus(
            voltage_v=volts,
            current_a=amps,
            power_w=watts,
            output_on=output_on,
            mode=mode,
            channel=ch,
        )

    def _poll_loop(self) -> None:
        while self._running:
            start = time.monotonic()
            try:
                status = self._poll_once()
                self._last_status = status
                if self._status_callback:
                    try:
                        self._status_callback(status)
                    except Exception:
                        pass
            except (OSError, ValueError) as e:
                if self._running:
                    self._report_error(f"Charger poll failed: {e}")
            remaining = self.POLL_INTERVAL - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _report_error(self, message: str) -> None:
        if self._error_callback:
            try:
                self._error_callback(message)
            except Exception:
                pass
