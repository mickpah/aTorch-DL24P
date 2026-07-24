"""Rigol DP832A power supply driver - SCPI over LAN via ScpiTransport.

The generic plumbing (socket, framing, lock-timeout commands, poll thread,
stale-status invalidation) lives in scpi_transport.py; this class supplies
only DP832A-specific SCPI strings and status parsing. Status callbacks fire
on the poll thread - GUI consumers must marshal through a Qt Signal.
"""

from typing import Callable, Optional

from .dp832a_protocol import ChargerStatus, DP832AProtocol
from .scpi_transport import LanScpiLink, ScpiError, ScpiTransport


class ChargerError(ScpiError):
    """Raised on DP832A connection or identification failures."""


class RigolDP832A:
    DEFAULT_PORT = 5555
    POLL_INTERVAL = 1.0  # seconds
    SOCKET_TIMEOUT = 2.0  # seconds
    GUI_LOCK_TIMEOUT = 1.0  # seconds

    def __init__(self, transport: Optional[ScpiTransport] = None) -> None:
        # transport injection is a test seam; connect() builds a LAN one.
        self._transport = transport
        self._channel = 1
        self._host: Optional[str] = None
        self._status_callback: Optional[Callable[[ChargerStatus], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None
        if transport is not None:
            self._apply_callbacks()

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected if self._transport else False

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def identity(self) -> str:
        return self._transport.identity if self._transport else ""

    @property
    def channel(self) -> int:
        return self._channel

    @property
    def last_status(self) -> Optional[ChargerStatus]:
        return self._transport.last_status if self._transport else None

    def set_channel(self, channel: int) -> None:
        DP832AProtocol.check_channel(channel)
        self._channel = channel

    def set_status_callback(self, callback: Callable[[ChargerStatus], None]) -> None:
        self._status_callback = callback
        self._apply_callbacks()

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback
        self._apply_callbacks()

    def connect(self, host: str, port: int = DEFAULT_PORT) -> bool:
        if self.is_connected:
            return True
        transport = self._transport or ScpiTransport(
            LanScpiLink(host, port, timeout=self.SOCKET_TIMEOUT),
            poll_interval=self.POLL_INTERVAL,
            lock_timeout=self.GUI_LOCK_TIMEOUT,
        )
        try:
            transport.connect(
                DP832AProtocol.parse_idn, describe=f"DP832A at {host}:{port}"
            )
        except ScpiError as e:
            raise ChargerError(str(e)) from e
        self._transport = transport
        self._apply_callbacks()
        self._host = host
        transport.start_polling(self._poll_once)
        return True

    def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.disconnect()

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
        if self._transport is None:
            return False
        return self._transport.command(cmd)

    def _apply_callbacks(self) -> None:
        if self._transport is None:
            return
        if self._status_callback is not None:
            self._transport.set_status_callback(self._status_callback)
        if self._error_callback is not None:
            self._transport.set_error_callback(self._error_callback)

    def _poll_once(self) -> ChargerStatus:
        """Read one status snapshot; runs under the transport lock."""
        proto = DP832AProtocol
        transport = self._transport

        def read() -> ChargerStatus:
            ch = self._channel
            volts, amps, watts = proto.parse_measure_all(
                transport.query(proto.cmd_measure_all(ch))
            )
            output_on = proto.parse_output_state(
                transport.query(proto.cmd_query_output(ch))
            )
            mode = (
                proto.parse_mode(transport.query(proto.cmd_query_mode(ch)))
                if output_on
                else "UR"
            )
            return ChargerStatus(
                voltage_v=volts,
                current_a=amps,
                power_w=watts,
                output_on=output_on,
                mode=mode,
                channel=ch,
            )

        return transport.run_locked(read)
