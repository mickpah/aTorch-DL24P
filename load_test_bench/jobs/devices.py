"""Device role protocols and the thread-safe device registry.

Roles, not concrete drivers: anything satisfying LoadDevice can be the load
(DL24 over USB HID today, a SCPI electronic load later). reset_counters and
device-side accumulators are DL24 conveniences - treat them as optional
capabilities; the engine integrates capacity in software when absent.
"""

import threading
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class LoadDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[object]: ...
    def set_mode(self, mode: int, value: Optional[float] = None) -> bool: ...
    def set_current(self, current_a: float) -> bool: ...
    def set_resistance(self, resistance_ohm: float) -> bool: ...
    def set_voltage_cutoff(self, voltage: float) -> bool: ...
    def reset_counters(self) -> bool: ...
    def turn_on(self) -> bool: ...
    def turn_off(self) -> bool: ...


@runtime_checkable
class PsuDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[object]: ...
    def set_voltage(self, volts: float) -> bool: ...
    def set_current(self, amps: float) -> bool: ...
    def set_ovp(self, volts: float) -> bool: ...
    def output_on(self) -> bool: ...
    def output_off(self) -> bool: ...


@dataclass
class MeterStatus:
    """Snapshot from an independent measurement instrument (e.g. SCPI DMM)."""

    voltage_v: float
    current_a: Optional[float] = None


@runtime_checkable
class MeterDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[MeterStatus]: ...


class DeviceRegistry:
    """Currently connected device per role. Thread-safe; ownership stays
    with whoever connected the device (MainWindow / panels)."""

    ROLES = ("load", "psu", "meter")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict = {}

    def register(self, role: str, device: object) -> None:
        if role not in self.ROLES:
            raise ValueError(f"Unknown device role: {role}")
        with self._lock:
            self._devices[role] = device

    def unregister(self, role: str) -> None:
        with self._lock:
            self._devices.pop(role, None)

    def get(self, role: str) -> Optional[object]:
        with self._lock:
            return self._devices.get(role)

    @property
    def load(self):
        return self.get("load")

    @property
    def psu(self):
        return self.get("psu")

    @property
    def meter(self):
        return self.get("meter")
