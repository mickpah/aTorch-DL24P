"""SCPI voltage-meter driver over ScpiTransport (LAN or USB).

Profile-driven: the MeterProfile supplies the IDN check, the one-time setup
commands, and the measure query. Mirrors RigolDP832A's structure. Status
callbacks fire on the poll thread - GUI consumers must marshal through a Qt
Signal. The meter is read-only (no outputs), so it is never actuated by the
safety layer.
"""

from typing import Callable, Optional

from ..jobs.devices import MeterStatus
from .meter_protocol import MeterProfile, make_idn_verifier, parse_measurement
from .scpi_transport import (
    LanScpiLink,
    ScpiError,
    ScpiTransport,
    UsbScpiLink,
)


class MeterError(ScpiError):
    """Raised on meter connection or identification failures."""


class ScpiMeter:
    SOCKET_TIMEOUT = 2.0
    GUI_LOCK_TIMEOUT = 1.0

    def __init__(self, transport: Optional[ScpiTransport] = None) -> None:
        self._transport = transport
        self._profile: Optional[MeterProfile] = None
        self._status_callback: Optional[Callable[[MeterStatus], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None
        if transport is not None:
            self._apply_callbacks()

    @property
    def is_connected(self) -> bool:
        transport = self._transport
        return transport.is_connected if transport else False

    @property
    def identity(self) -> str:
        transport = self._transport
        return transport.identity if transport else ""

    @property
    def last_status(self) -> Optional[MeterStatus]:
        transport = self._transport
        return transport.last_status if transport else None

    @property
    def profile(self) -> Optional[MeterProfile]:
        return self._profile

    def set_status_callback(self, callback: Callable[[MeterStatus], None]) -> None:
        self._status_callback = callback
        self._apply_callbacks()

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback
        self._apply_callbacks()

    def connect_lan(self, host: str, port: int, profile: MeterProfile, _link=None) -> bool:
        link = _link if _link is not None else LanScpiLink(host, port, timeout=self.SOCKET_TIMEOUT)
        return self._connect(link, profile, describe=f"meter at {host}:{port}")

    def connect_usb(self, port: str, profile: MeterProfile, baudrate: int = 115200, _link=None) -> bool:
        link = _link if _link is not None else UsbScpiLink(port, baudrate=baudrate, timeout=self.SOCKET_TIMEOUT)
        return self._connect(link, profile, describe=f"meter on {port}")

    def _connect(self, link, profile: MeterProfile, describe: str) -> bool:
        if self.is_connected:
            return True
        transport = self._transport or ScpiTransport(
            link, lock_timeout=self.GUI_LOCK_TIMEOUT
        )
        try:
            transport.connect(make_idn_verifier(profile), describe=describe)
        except ScpiError as e:
            raise MeterError(str(e)) from e
        self._transport = transport
        self._profile = profile
        self._apply_callbacks()
        for command in profile.setup_commands:
            if not transport.command(command):
                transport.disconnect()
                # Clear the stale transport so a retry (possibly a new
                # port/host) builds a fresh one instead of reusing this link.
                self._transport = None
                self._profile = None
                raise MeterError(f"Failed to configure meter: {command}")
        transport.start_polling(self._poll_once)
        return True

    def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.disconnect()
            self._transport = None
        self._profile = None

    def _apply_callbacks(self) -> None:
        if self._transport is None:
            return
        if self._status_callback is not None:
            self._transport.set_status_callback(self._status_callback)
        if self._error_callback is not None:
            self._transport.set_error_callback(self._error_callback)

    def _poll_once(self) -> MeterStatus:
        """Read one voltage measurement; runs under the transport lock."""
        transport = self._transport
        command = self._profile.measure_command

        def read() -> MeterStatus:
            return MeterStatus(voltage_v=parse_measurement(transport.query(command)))

        return transport.run_locked(read)
