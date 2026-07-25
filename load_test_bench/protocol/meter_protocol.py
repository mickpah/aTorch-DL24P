"""SCPI voltage-meter profiles and measurement parsing (pure, no I/O).

A MeterProfile describes how to drive one class of SCPI DMM: the setup
commands to issue once at connect, the query that returns the measured
voltage, and an optional IDN substring match. New instruments are added by
appending a profile here (battery-preset style) - no driver changes needed.
"""

import re
from dataclasses import dataclass
from typing import Callable, Tuple

_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class MeterProfile:
    key: str
    label: str
    setup_commands: Tuple[str, ...]
    measure_command: str
    idn_contains: Tuple[str, ...] = ()
    default_transport: str = "usb"  # "usb" | "lan"
    default_lan_port: int = 5555


METER_PROFILES = {
    "hds200": MeterProfile(
        key="hds200",
        label="OWON HDS200 (DMM, USB)",
        setup_commands=(":DMM:CONFigure:VOLTage DC", ":DMM:AUTO ON"),
        measure_command=":DMM:MEAS?",
        idn_contains=("OWON", "HDS"),
        default_transport="usb",
    ),
    "generic_scpi_dmm": MeterProfile(
        key="generic_scpi_dmm",
        label="Generic SCPI DMM (MEAS:VOLT:DC?, LAN)",
        setup_commands=(),
        measure_command=":MEASure:VOLTage:DC?",
        idn_contains=(),
        default_transport="lan",
        default_lan_port=5555,
    ),
}


def parse_measurement(response: str) -> float:
    """Extract the numeric voltage from a measure-query response.

    Tolerates surrounding whitespace, unit suffixes, and scientific notation.
    """
    match = _FLOAT_RE.search(response)
    if match is None:
        raise ValueError(f"No numeric measurement in response: {response!r}")
    return float(match.group())


def make_idn_verifier(profile: MeterProfile) -> Callable[[str], bool]:
    """Predicate for ScpiTransport.connect: accept the instrument's *IDN?.

    Empty idn_contains accepts any instrument that answers *IDN? (useful for
    DMMs whose IDN string is not known in advance).
    """
    wanted = tuple(s.upper() for s in profile.idn_contains)

    def verify(idn: str) -> bool:
        if not wanted:
            return True
        upper = idn.upper()
        return any(token in upper for token in wanted)

    return verify
