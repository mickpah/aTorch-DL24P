"""SCPI protocol for the Rigol DP832A power supply (LAN, raw SCPI on TCP 5555).

Pure string building/parsing - no I/O. The socket transport lives in
rigol_dp832a.py, mirroring the atorch_protocol.py / device.py split.
"""

from dataclasses import dataclass
from typing import Tuple

# channel -> (max_voltage, max_current). CH1/CH2 are 30 V / 3 A rails, CH3 is
# 5 V / 3 A; limits include the small setpoint overrange the instrument accepts.
CHANNEL_LIMITS = {
    1: (32.0, 3.2),
    2: (32.0, 3.2),
    3: (5.3, 3.2),
}


@dataclass
class ChargerStatus:
    """Snapshot of one DP832A channel."""

    voltage_v: float
    current_a: float
    power_w: float
    output_on: bool
    mode: str  # "CC", "CV", or "UR" (unregulated / output off)
    channel: int


class DP832AProtocol:
    """Builds SCPI command strings and parses responses for the DP832A."""

    @staticmethod
    def check_channel(channel: int) -> None:
        if channel not in CHANNEL_LIMITS:
            raise ValueError(f"Invalid channel: {channel} (must be 1-3)")

    @staticmethod
    def cmd_idn() -> str:
        return "*IDN?"

    @classmethod
    def cmd_set_voltage(cls, channel: int, volts: float) -> str:
        cls.check_channel(channel)
        max_v, _ = CHANNEL_LIMITS[channel]
        if not 0.0 <= volts <= max_v:
            raise ValueError(f"Voltage {volts} out of range 0-{max_v} V for CH{channel}")
        return f":SOUR{channel}:VOLT {volts:.3f}"

    @classmethod
    def cmd_set_current(cls, channel: int, amps: float) -> str:
        cls.check_channel(channel)
        _, max_a = CHANNEL_LIMITS[channel]
        if not 0.0 <= amps <= max_a:
            raise ValueError(f"Current {amps} out of range 0-{max_a} A for CH{channel}")
        return f":SOUR{channel}:CURR {amps:.3f}"

    @classmethod
    def cmd_set_output(cls, channel: int, on: bool) -> str:
        cls.check_channel(channel)
        return f":OUTP CH{channel},{'ON' if on else 'OFF'}"

    @classmethod
    def cmd_query_output(cls, channel: int) -> str:
        cls.check_channel(channel)
        return f":OUTP? CH{channel}"

    @classmethod
    def cmd_measure_all(cls, channel: int) -> str:
        cls.check_channel(channel)
        return f":MEAS:ALL? CH{channel}"

    @classmethod
    def cmd_query_mode(cls, channel: int) -> str:
        cls.check_channel(channel)
        return f":OUTP:MODE? CH{channel}"

    @classmethod
    def cmd_set_ovp_value(cls, channel: int, volts: float) -> str:
        cls.check_channel(channel)
        return f":OUTP:OVP:VAL CH{channel},{volts:.3f}"

    @classmethod
    def cmd_set_ovp_state(cls, channel: int, on: bool) -> str:
        cls.check_channel(channel)
        return f":OUTP:OVP CH{channel},{'ON' if on else 'OFF'}"

    @staticmethod
    def parse_idn(response: str) -> bool:
        """True if the *IDN? response identifies a Rigol DP832/DP832A."""
        parts = response.strip().split(",")
        if len(parts) < 2:
            return False
        return "RIGOL" in parts[0].upper() and parts[1].strip().upper().startswith("DP832")

    @staticmethod
    def parse_measure_all(response: str) -> Tuple[float, float, float]:
        """Parse a ':MEAS:ALL?' response 'V,I,P' into (volts, amps, watts)."""
        parts = response.strip().split(",")
        if len(parts) != 3:
            raise ValueError(f"Bad MEAS:ALL response: {response!r}")
        return float(parts[0]), float(parts[1]), float(parts[2])

    @staticmethod
    def parse_output_state(response: str) -> bool:
        return response.strip().upper() == "ON"

    @staticmethod
    def parse_mode(response: str) -> str:
        mode = response.strip().upper()
        if mode not in ("CC", "CV", "UR"):
            raise ValueError(f"Bad mode response: {response!r}")
        return mode
