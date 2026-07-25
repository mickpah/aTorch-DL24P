# SCPI Voltage Meter (Cable-Drop Mitigation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional SCPI voltage meter (OWON HDS200 over USB by default, any SCPI DMM over USB or LAN via profiles) that senses true battery-terminal voltage, logs it alongside every engine-run reading, and can override the load/PSU-measured voltage for discharge cutoff decisions — mitigating cable IR drop.

**Architecture:** The job engine already carries the meter seam end to end (`MeterStatus`, the `"meter"` registry role, `PhaseContext.meter`, `Phase._meter_voltage` keyed on a `voltage_source` param, and an unused `readings.aux_voltage_v` column from migration 1). This plan fills that seam: a USB link for the existing `ScpiTransport`, a profile-driven `ScpiMeter` driver, aux-voltage persistence/export, engine capture, facade cutoff-sourcing, a settings-backed connection dialog, and a live readout on the charger panel.

**Tech Stack:** Python ≥3.10 stdlib `socket` (LAN) + `pyserial` (USB CDC — already a dependency), PySide6, pytest.

**Background — HDS200 SCPI facts (from `docs/HDS200_Series_SCPI_Protocol.pdf`):**
- Handheld scope/DMM/AWG; **connects over USB** (CDC serial — appears as a serial port). LAN is not offered on this model, so USB is its default transport; the LAN path exists for other SCPI DMMs.
- `*IDN?` → `<Manufacturer>,<model>,<serial>,<version>` (the PDF shows placeholder `XXXX,XXXXXXX,...`; the real unit returns an OWON/HDS string — treat IDN matching as best-effort, see the "generic" profile).
- DMM subsystem: `:DMM:CONFigure:VOLTage DC` (set DC-volts function), `:DMM:AUTO ON` (auto range), `:DMM:MEAS?` (query the displayed measured value). Response is the numeric value (parse tolerantly — strip any unit/whitespace, accept scientific notation).
- A standard bench DMM (Keysight/Rigol/Siglent) instead uses `:MEASure:VOLTage:DC?` — covered by a second built-in profile so "other SCPI instruments" is real, not aspirational.

**Scope decisions (assumptions — flag to the user if wrong):**
- Two built-in instrument profiles ship: `hds200` (OWON DMM subsystem, USB) and `generic_scpi_dmm` (standard `MEASure:VOLTage:DC?`, LAN). Adding another instrument = adding a `MeterProfile` entry (battery-preset style); a custom-profile authoring **UI** is future work.
- Meter voltage overrides cutoff only on the **engine/facade discharge path** (Battery Capacity, Power Bank — the tests where cable drop under load actually distorts the cutoff). The Battery Load / Charger-Load panels still run their own pre-engine QTimer sweeps; wiring the meter into them arrives when they migrate to the engine (documented future work).
- **Aux-voltage logging** happens for every engine-created session (any facade-run test). The manual DL24 control-panel logging path (`main_window._current_session`) is a separate reading-construction site and is **not** in scope — noted as future work.
- The charger (DP832A) panel gets a **live meter readout only** (true battery voltage shown while charging); closed-loop charge compensation against the meter is explicitly out of scope.
- If the meter is enabled-for-cutoff but drops out mid-test, `Phase._meter_voltage` returns `None` and the cores fall back to the load's own voltage. Cable drop makes the load read *lower* than the true battery, so the fallback cuts off *early* (conservative — never over-discharges). This is acceptable; the status bar surfaces the lost-meter condition.

## Global Constraints

- Read the design spec's meter section first: `docs/superpowers/specs/2026-07-24-job-engine-design.md` ("Meter role", "SCPI transport extraction"). It governs on any ambiguity.
- **No Qt imports** under `load_test_bench/jobs/` or `load_test_bench/protocol/` (testability + Prefect-seam boundary).
- No new dependencies — LAN via stdlib `socket`, USB via the existing `pyserial>=3.5`.
- **No Claude / Co-Authored-By trailers in any commit** in this repo (the user purged all attribution on 2026-07-25). This overrides the default commit-trailer instruction; pass the rule to every subagent that commits.
- Device status/poll callbacks run on poll threads — GUI updates only via Qt Signals (CLAUDE.md "Qt Threading Safety"). The meter is read-only (no outputs), so it is NOT wired into the SafetySupervisor.
- GUI-initiated device commands use the transport's lock-timeout `command()` (never raises on I/O).
- Meter connection settings persist in `settings.json` under a `"meter"` key, using the load-merge-over-defaults / write-whole-dict pattern of `_load_notification_settings` / `_save_notification_settings`.
- Tests are pure-logic with injected fakes and no mock library, matching `tests/test_scpi_transport.py` (`FakeLink`) and `tests/test_charge_monitor.py` style: pytest `TestXxx` classes, `test_<behavior>` methods with docstrings.
- Run everything via uv: `uv run --extra dev pytest`. The suite is **279 tests** at the start of this plan; it must stay green after every task.
- Existing DB columns are legacy-named (`voltage`, `current`, `temperature_c`, `runtime_seconds`) — do not rename them; `aux_voltage_v` is the one new column and it already exists (migration 1).
- Commit messages: imperative sentence, no `feat:` prefix.

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `load_test_bench/protocol/scpi_transport.py` (modify) | 1 | Add `UsbScpiLink` (pyserial CDC) + `list_serial_ports()` |
| `load_test_bench/protocol/meter_protocol.py` (create) | 2 | `MeterProfile`, `METER_PROFILES`, `parse_measurement`, `make_idn_verifier` — pure |
| `load_test_bench/protocol/scpi_meter.py` (create) | 3 | `ScpiMeter` driver (duck-types `MeterDevice`), `MeterError` |
| `load_test_bench/data/models.py` (modify) | 4 | `Reading.aux_voltage_v` field + `to_dict` |
| `load_test_bench/data/database.py` (modify) | 4 | Persist/read `aux_voltage_v` in `add_reading`/`add_readings_batch`/`get_readings` |
| `load_test_bench/data/export.py` (modify) | 5 | `aux_voltage_v` in CSV/JSON/Excel exporters (None-guarded) |
| `load_test_bench/jobs/engine.py` (modify) | 6 | `_capture_reading` reads the registry meter into `aux_voltage_v` |
| `load_test_bench/automation/test_runner.py` (modify) | 7 | `voltage_source` attribute + `profile_to_spec` param threads it into phase params |
| `load_test_bench/config.py` (modify) | 8 | `MeterSettings`, `load_meter_settings`, `save_meter_settings` — pure |
| `load_test_bench/gui/voltage_monitor_dialog.py` (create) | 9 | Connection/config dialog for the meter |
| `load_test_bench/gui/main_window.py` (modify) | 10 | Construct meter, Device-menu action, register on connect, cutoff flag, status indicator, closeEvent shutdown |
| `load_test_bench/gui/dp832a_charger_panel.py` (modify) | 10 | Live meter-voltage readout while charging |
| `CLAUDE.md`, `CHANGELOG.md`, `TODO.md`, `README.md` (modify) | 11 | Docs |
| `tests/test_usb_scpi_link.py` … `tests/test_meter_config.py` (create) | 1–8 | Unit tests per task |

---

### Task 1: USB SCPI link (`UsbScpiLink`) + serial-port discovery

**Files:**
- Modify: `load_test_bench/protocol/scpi_transport.py`
- Test: `tests/test_usb_scpi_link.py`

**Interfaces:**
- Consumes: the existing `ScpiLink` Protocol (`open`/`close`/`send`/`recv`) in the same file.
- Produces (Task 3 consumes): `UsbScpiLink(port: str, baudrate: int = 115200, timeout: float = 2.0, serial_obj=None)` satisfying `ScpiLink` — `serial_obj` injects a pre-opened serial-like object for tests (then `open()` is a no-op). `recv(max_bytes)` returns buffered bytes immediately when available, else does one blocking ≤timeout read, raising `OSError` on timeout (matches the LAN link's "empty means failure" contract). Plus `list_serial_ports() -> list[tuple[str, str]]` returning `(device, description)` pairs.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_usb_scpi_link.py`:

```python
"""Tests for the USB (pyserial CDC) SCPI link."""

import pytest

from load_test_bench.protocol.scpi_transport import ScpiTransport, UsbScpiLink


class FakeSerial:
    """Stand-in for a pyserial Serial: buffers a scripted reply."""

    def __init__(self, reply: bytes = b""):
        self._rx = reply
        self.written = b""
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def write(self, data):
        self.written += data

    def read(self, n):
        chunk, self._rx = self._rx[:n], self._rx[n:]
        return chunk

    def close(self):
        self.closed = True


class TestUsbScpiLink:
    def test_send_writes_to_serial(self):
        fake = FakeSerial()
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        link.open()  # no-op with injected serial
        link.send(b"*IDN?\n")
        assert fake.written == b"*IDN?\n"

    def test_recv_returns_buffered_bytes(self):
        fake = FakeSerial(reply=b"4.187\n")
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        assert link.recv(4096) == b"4.187\n"

    def test_recv_timeout_raises_oserror(self):
        """No data buffered and a blocking read returning empty is a timeout."""
        fake = FakeSerial(reply=b"")
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        with pytest.raises(OSError):
            link.recv(4096)

    def test_close_closes_serial(self):
        fake = FakeSerial()
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        link.close()
        assert fake.closed is True
        assert link._serial is None

    def test_send_before_open_raises(self):
        link = UsbScpiLink("/dev/ttyUSB0")
        with pytest.raises(OSError):
            link.send(b"x\n")


class TestTransportOverUsb:
    def test_query_round_trip_through_transport(self):
        """A full ScpiTransport query works over the USB link (line-framed)."""
        fake = FakeSerial(reply=b"OWON,HDS242,2128009,V2.1.1.5\n")
        transport = ScpiTransport(UsbScpiLink("/dev/ttyUSB0", serial_obj=fake))
        transport.connect(lambda idn: "OWON" in idn, describe="meter")
        assert transport.is_connected is True
        assert "OWON" in transport.identity
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_usb_scpi_link.py -v`
Expected: FAIL — `ImportError: cannot import name 'UsbScpiLink'`

- [ ] **Step 3: Implement in `scpi_transport.py`**

Add after the `LanScpiLink` class (before `class ScpiTransport`):

```python
class UsbScpiLink:
    """SCPI over a USB CDC serial port (e.g. OWON HDS200, appears as a serial
    device). Uses pyserial. A pre-opened serial-like object may be injected
    for tests; open() is then a no-op.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0, serial_obj=None) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._serial = serial_obj

    def open(self) -> None:
        if self._serial is None:
            import serial  # lazy: pyserial is a dependency but keep import local
            self._serial = serial.Serial(
                self._port, self._baudrate, timeout=self._timeout
            )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except OSError:
                pass
        self._serial = None

    def send(self, data: bytes) -> None:
        if self._serial is None:
            raise OSError("Serial link not open")
        self._serial.write(data)

    def recv(self, max_bytes: int) -> bytes:
        if self._serial is None:
            raise OSError("Serial link not open")
        # Return buffered bytes immediately; otherwise block for one byte up to
        # the timeout so a short reply doesn't wait the full read() timeout.
        waiting = getattr(self._serial, "in_waiting", 0)
        if waiting:
            return self._serial.read(min(waiting, max_bytes))
        one = self._serial.read(1)
        if not one:
            raise OSError("Serial read timed out")
        return one


def list_serial_ports() -> list:
    """Available serial ports as (device, description) pairs (for the meter UI)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_usb_scpi_link.py -v`
Expected: all PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/protocol/scpi_transport.py tests/test_usb_scpi_link.py
git commit -m "Add USB CDC SCPI link and serial-port discovery"
```

---

### Task 2: Meter protocol and instrument profiles (`meter_protocol.py`)

**Files:**
- Create: `load_test_bench/protocol/meter_protocol.py`
- Test: `tests/test_meter_protocol.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces (Task 3 consumes):
  - `MeterProfile` (frozen): `key: str`, `label: str`, `setup_commands: tuple`, `measure_command: str`, `idn_contains: tuple = ()`, `default_transport: str = "usb"`, `default_lan_port: int = 5555`.
  - `METER_PROFILES: dict[str, MeterProfile]` with keys `"hds200"` and `"generic_scpi_dmm"`.
  - `parse_measurement(response: str) -> float` — tolerant float extraction, raises `ValueError` on no numeric content.
  - `make_idn_verifier(profile: MeterProfile) -> Callable[[str], bool]` — returns a predicate: `True` if `idn_contains` is empty (accept any responding instrument) or any substring (case-insensitive) is present.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_meter_protocol.py`:

```python
"""Tests for the SCPI meter profiles and measurement parsing."""

import pytest

from load_test_bench.protocol.meter_protocol import (
    METER_PROFILES,
    make_idn_verifier,
    parse_measurement,
)


class TestParseMeasurement:
    def test_plain_float(self):
        assert parse_measurement("4.187\n") == 4.187

    def test_scientific_notation(self):
        assert parse_measurement("4.187000e+00\n") == pytest.approx(4.187)

    def test_strips_units_and_whitespace(self):
        assert parse_measurement("  4.187 V \r\n") == 4.187

    def test_negative(self):
        assert parse_measurement("-0.512") == -0.512

    def test_no_number_raises(self):
        with pytest.raises(ValueError):
            parse_measurement("OVERLOAD")


class TestProfiles:
    def test_hds200_profile(self):
        """OWON HDS200 uses its DMM subsystem over USB."""
        p = METER_PROFILES["hds200"]
        assert p.default_transport == "usb"
        assert p.measure_command == ":DMM:MEAS?"
        assert ":DMM:CONFigure:VOLTage DC" in p.setup_commands
        assert ":DMM:AUTO ON" in p.setup_commands

    def test_generic_profile_uses_standard_scpi(self):
        """A standard bench DMM uses MEASure:VOLTage:DC? and no setup."""
        p = METER_PROFILES["generic_scpi_dmm"]
        assert p.measure_command == ":MEASure:VOLTage:DC?"
        assert p.setup_commands == ()
        assert p.default_transport == "lan"

    def test_all_profiles_have_unique_keys_matching_dict(self):
        for key, profile in METER_PROFILES.items():
            assert profile.key == key


class TestIdnVerifier:
    def test_matches_substring_case_insensitive(self):
        verify = make_idn_verifier(METER_PROFILES["hds200"])
        assert verify("owon,hds242,2128009,V2.1.1.5") is True

    def test_rejects_non_matching_when_constrained(self):
        verify = make_idn_verifier(METER_PROFILES["hds200"])
        assert verify("RIGOL TECHNOLOGIES,DP832A,X,Y") is False

    def test_empty_constraint_accepts_any_responding_instrument(self):
        verify = make_idn_verifier(METER_PROFILES["generic_scpi_dmm"])
        assert verify("ANYTHING,MODEL,SN,VER") is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_meter_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.protocol.meter_protocol'`

- [ ] **Step 3: Implement `meter_protocol.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_meter_protocol.py -v`
Expected: all PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/protocol/meter_protocol.py tests/test_meter_protocol.py
git commit -m "Add SCPI meter profiles and measurement parsing"
```

---

### Task 3: ScpiMeter driver (`scpi_meter.py`)

**Files:**
- Create: `load_test_bench/protocol/scpi_meter.py`
- Test: `tests/test_scpi_meter.py`

**Interfaces:**
- Consumes: `ScpiTransport`, `LanScpiLink`, `UsbScpiLink`, `ScpiError` (Task 1); `MeterProfile`, `parse_measurement`, `make_idn_verifier` (Task 2); `MeterStatus` from `jobs/devices.py`.
- Produces (Tasks 6/10 consume): `MeterError(ScpiError)`; `ScpiMeter(transport=None)` duck-typing `MeterDevice`:
  - properties `is_connected: bool`, `identity: str`, `last_status: Optional[MeterStatus]`, `profile: Optional[MeterProfile]`
  - `set_status_callback(cb)`, `set_error_callback(cb)`
  - `connect_lan(host: str, port: int, profile: MeterProfile) -> bool` and `connect_usb(port: str, profile: MeterProfile, baudrate: int = 115200) -> bool` — both raise `MeterError` on failure, run `profile.setup_commands` after IDN verification, then start polling
  - `disconnect() -> None`
  - `_poll_once() -> MeterStatus` (measure query under the transport lock)

Note: `jobs/devices.py` imports nothing from `protocol/`, and `protocol/scpi_meter.py` importing `MeterStatus` from `jobs/devices.py` creates no cycle (devices.py has no protocol import). Verify with the import in Step 4's test run.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scpi_meter.py`:

```python
"""Tests for the ScpiMeter driver over a scripted fake link."""

from load_test_bench.jobs.devices import MeterDevice, MeterStatus
from load_test_bench.protocol.meter_protocol import METER_PROFILES
from load_test_bench.protocol.scpi_meter import ScpiMeter
from load_test_bench.protocol.scpi_transport import ScpiTransport


class FakeLink:
    """Scripted ScpiLink for a DMM: *IDN? plus a measure reply."""

    def __init__(self, responses=None):
        self.responses = {
            "*IDN?": "OWON,HDS242,2128009,V2.1.1.5\n",
            ":DMM:MEAS?": "4.187\n",
        }
        if responses:
            self.responses.update(responses)
        self.sent = []
        self._pending = b""

    def open(self):
        pass

    def close(self):
        pass

    def send(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, max_bytes):
        data, self._pending = self._pending, b""
        return data


def make_meter(link=None):
    """Meter wired to a fake link's transport, bypassing connect_*/polling."""
    transport = ScpiTransport(link if link is not None else FakeLink())
    transport._connected = True
    meter = ScpiMeter(transport=transport)
    meter._profile = METER_PROFILES["hds200"]
    return meter


class TestConformance:
    def test_scpi_meter_is_a_meter_device(self):
        assert isinstance(ScpiMeter(), MeterDevice)


class TestPolling:
    def test_poll_once_parses_voltage(self):
        meter = make_meter()
        status = meter._poll_once()
        assert isinstance(status, MeterStatus)
        assert status.voltage_v == 4.187
        assert meter._transport._link.sent == [":DMM:MEAS?"]

    def test_generic_profile_uses_standard_measure(self):
        link = FakeLink({":MEASure:VOLTage:DC?": "3.702\n"})
        transport = ScpiTransport(link)
        transport._connected = True
        meter = ScpiMeter(transport=transport)
        meter._profile = METER_PROFILES["generic_scpi_dmm"]
        status = meter._poll_once()
        assert status.voltage_v == 3.702
        assert link.sent == [":MEASure:VOLTage:DC?"]


class TestConnect:
    def test_connect_runs_setup_then_polls(self):
        """connect verifies IDN, issues setup commands, then starts polling."""
        link = FakeLink()
        meter = ScpiMeter()
        assert meter.connect_lan("10.0.0.9", 5555, METER_PROFILES["hds200"],
                                 _link=link) is True
        # setup commands were sent after the *IDN? handshake
        assert ":DMM:CONFigure:VOLTage DC" in link.sent
        assert ":DMM:AUTO ON" in link.sent
        assert meter.is_connected is True
        meter.disconnect()

    def test_connect_rejects_wrong_instrument(self):
        import pytest

        from load_test_bench.protocol.scpi_meter import MeterError

        link = FakeLink({"*IDN?": "RIGOL TECHNOLOGIES,DP832A,X,Y\n"})
        meter = ScpiMeter()
        with pytest.raises(MeterError):
            meter.connect_lan("10.0.0.9", 5555, METER_PROFILES["hds200"], _link=link)

    def test_commands_without_transport_are_safe(self):
        """A never-connected meter reports no status rather than raising."""
        meter = ScpiMeter()
        assert meter.last_status is None
        assert meter.is_connected is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_scpi_meter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.protocol.scpi_meter'`

- [ ] **Step 3: Implement `scpi_meter.py`**

```python
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
        return self._transport.is_connected if self._transport else False

    @property
    def identity(self) -> str:
        return self._transport.identity if self._transport else ""

    @property
    def last_status(self) -> Optional[MeterStatus]:
        return self._transport.last_status if self._transport else None

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
            transport.command(command)
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_scpi_meter.py -v`
Expected: all PASS (6 tests)

Run: `uv run --extra dev pytest`
Expected: everything green (no import cycle; `jobs.devices` still imports cleanly).

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/protocol/scpi_meter.py tests/test_scpi_meter.py
git commit -m "Add profile-driven SCPI voltage meter driver"
```

---

### Task 4: Persist aux voltage (`Reading` field + database)

**Files:**
- Modify: `load_test_bench/data/models.py`
- Modify: `load_test_bench/data/database.py`
- Test: `tests/test_aux_voltage_persistence.py`

**Interfaces:**
- Consumes: existing `Reading`, `Database` (the `readings.aux_voltage_v` column already exists from migration 1).
- Produces (Tasks 5/6 consume): `Reading.aux_voltage_v: Optional[float] = None` (in `to_dict` as `"aux_voltage_v"`); `Database.add_reading`/`add_readings_batch` persist it; `Database.get_readings` restores it (`None` when the column is NULL).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aux_voltage_persistence.py`:

```python
"""Tests that a meter's aux voltage round-trips through the readings table."""

from datetime import datetime

from load_test_bench.data.database import Database
from load_test_bench.data.models import Reading, TestSession


def make_reading(aux=None):
    return Reading(
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        voltage_v=3.70, current_a=1.0, power_w=3.70, energy_wh=0.5,
        capacity_mah=500.0, mosfet_temp_c=40, ext_temp_c=25,
        aux_voltage_v=aux,
    )


def make_session(db):
    session = TestSession(name="s", start_time=datetime(2026, 1, 1, 10, 0, 0))
    session.id = db.create_session(session)
    return session.id


class TestAuxVoltagePersistence:
    def test_reading_has_aux_field_defaulting_none(self):
        assert make_reading().aux_voltage_v is None
        assert make_reading(aux=3.81).aux_voltage_v == 3.81

    def test_to_dict_includes_aux(self):
        assert make_reading(aux=3.81).to_dict()["aux_voltage_v"] == 3.81

    def test_add_reading_round_trips_aux(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_reading(session_id, make_reading(aux=3.812), commit=True)
        readings = db.get_readings(session_id)
        assert len(readings) == 1
        assert readings[0].aux_voltage_v == 3.812
        db.close()

    def test_null_aux_reads_back_as_none(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_reading(session_id, make_reading(aux=None), commit=True)
        assert db.get_readings(session_id)[0].aux_voltage_v is None
        db.close()

    def test_batch_round_trips_aux(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_readings_batch(session_id, [make_reading(aux=3.80), make_reading(aux=3.79)])
        aux = [r.aux_voltage_v for r in db.get_readings(session_id)]
        assert aux == [3.80, 3.79]
        db.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_aux_voltage_persistence.py -v`
Expected: FAIL — `TypeError: Reading.__init__() got an unexpected keyword argument 'aux_voltage_v'`

- [ ] **Step 3a: Add the field in `models.py`**

In `Reading` (in `load_test_bench/data/models.py`), change:

```python
    runtime_s: int = 0
    id: Optional[int] = None
```

to:

```python
    runtime_s: int = 0
    aux_voltage_v: Optional[float] = None  # independent meter voltage (cable-drop mitigation)
    id: Optional[int] = None
```

In `Reading.to_dict`, add after the `"runtime_s": self.runtime_s,` line:

```python
            "aux_voltage_v": self.aux_voltage_v,
```

- [ ] **Step 3b: Persist in `database.py`**

In `add_reading`, change the INSERT column list and VALUES placeholder count:

```python
            INSERT INTO readings
            (session_id, timestamp, voltage, current, power, energy_wh,
             capacity_mah, temperature_c, ext_temperature_c, fan_speed_rpm,
             load_r_ohm, battery_r_ohm, runtime_seconds, aux_voltage_v)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

and add `reading.aux_voltage_v,` as the last value, immediately after `reading.runtime_s,`.

In `add_readings_batch`, make the identical INSERT change and add `r.aux_voltage_v,` as the last tuple element after `r.runtime_s,`.

In `get_readings`, add to the `Reading(...)` construction, after the `runtime_s=row["runtime_seconds"],` line:

```python
                    aux_voltage_v=row["aux_voltage_v"] if "aux_voltage_v" in row.keys() else None,
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_aux_voltage_persistence.py -v`
Expected: all PASS (5 tests)

Run: `uv run --extra dev pytest tests/test_database.py -v`
Expected: still green (existing DB tests construct Readings without aux; the new column defaults to NULL).

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/data/models.py load_test_bench/data/database.py tests/test_aux_voltage_persistence.py
git commit -m "Persist meter aux voltage in the readings table"
```

---

### Task 5: Export aux voltage (CSV / JSON / Excel)

**Files:**
- Modify: `load_test_bench/data/export.py`
- Test: `tests/test_export.py` (add cases)

**Interfaces:**
- Consumes: `Reading.aux_voltage_v` (Task 4).
- Produces: `aux_voltage_V` column in CSV, `aux_voltage_v` key in JSON, `aux_voltage_V` column in Excel — all None-safe (blank / null when no meter).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_export.py` (a new class at the end of the file):

```python
class TestAuxVoltageExport:
    """Meter aux voltage appears in exports, blank/null when absent."""

    def _session_with(self, aux):
        from datetime import datetime

        from load_test_bench.data.models import Reading, TestSession

        session = TestSession(name="aux", start_time=datetime(2026, 1, 1, 10, 0, 0))
        session.readings = [
            Reading(
                timestamp=datetime(2026, 1, 1, 10, 0, 1),
                voltage_v=3.70, current_a=1.0, power_w=3.70, energy_wh=0.5,
                capacity_mah=500.0, mosfet_temp_c=40, ext_temp_c=25,
                aux_voltage_v=aux,
            )
        ]
        return session

    def test_csv_has_aux_column_with_value(self, tmp_path):
        from load_test_bench.data.export import export_csv

        path = tmp_path / "out.csv"
        export_csv(self._session_with(3.812), path)
        text = path.read_text()
        assert "aux_voltage_V" in text.splitlines()[0]
        assert "3.812" in text

    def test_csv_blank_when_no_meter(self, tmp_path):
        from load_test_bench.data.export import export_csv

        path = tmp_path / "out.csv"
        export_csv(self._session_with(None), path)
        # header present, value cell empty (row ends with a trailing comma field)
        assert "aux_voltage_V" in path.read_text().splitlines()[0]

    def test_json_has_aux_key(self, tmp_path):
        import json

        from load_test_bench.data.export import export_json

        path = tmp_path / "out.json"
        export_json(self._session_with(3.812), path)
        data = json.loads(path.read_text())
        assert data["readings"][0]["aux_voltage_v"] == 3.812

    def test_json_aux_null_when_absent(self, tmp_path):
        import json

        from load_test_bench.data.export import export_json

        path = tmp_path / "out.json"
        export_json(self._session_with(None), path)
        assert json.loads(path.read_text())["readings"][0]["aux_voltage_v"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_export.py::TestAuxVoltageExport -v`
Expected: FAIL — assertions on the missing `aux_voltage_V` column / `aux_voltage_v` key.

- [ ] **Step 3: Implement in `export.py`**

In `export_csv`, add `"aux_voltage_V",` to the header `writer.writerow([...])` list (after `"ext_temp_C",`), and add this as the last cell of the per-reading `writer.writerow([...])`:

```python
                "" if reading.aux_voltage_v is None else f"{reading.aux_voltage_v:.3f}",
```

In `export_json`, add to the per-reading appended dict (after `"ext_temp_c": reading.ext_temp_c,`):

```python
            "aux_voltage_v": reading.aux_voltage_v,
```

In `export_excel`, add to its per-reading dict the same key/value pair (after the `ext_temp_c` entry), matching that function's existing dict style:

```python
            "aux_voltage_V": reading.aux_voltage_v,
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_export.py -v`
Expected: all PASS (existing 20 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/data/export.py tests/test_export.py
git commit -m "Export meter aux voltage in CSV, JSON, and Excel"
```

---

### Task 6: Engine captures the meter voltage

**Files:**
- Modify: `load_test_bench/jobs/engine.py`
- Test: `tests/test_job_executor.py` (add a case)

**Interfaces:**
- Consumes: `DeviceRegistry.meter` (already available), `MeterStatus` (via duck-typed `last_status.voltage_v`), `Reading.aux_voltage_v` (Task 4).
- Produces: every engine-captured reading carries `aux_voltage_v` = the registered meter's latest voltage (or `None` when no meter / no meter status).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_job_executor.py` — extend the `Harness` to accept an optional meter, and add a test. First, in the `Harness.__init__`, after `self.registry.register("load", self.load)`, the harness already exists; add a helper method to register a meter. Add this test class at the end of the file:

```python
class TestMeterCapture:
    def test_reading_carries_meter_voltage(self, tmp_path):
        """When a meter is registered, its voltage is logged as aux_voltage_v."""
        from load_test_bench.jobs.devices import MeterStatus
        from tests.fakes import FakeMeter

        harness = Harness(tmp_path)
        try:
            meter = FakeMeter()
            meter.status = MeterStatus(voltage_v=3.815)
            harness.registry.register("meter", meter)
            harness.executor.submit(discharge_spec())
            harness.run(0.0, 3.0)
            assert harness.readings, "expected at least one reading"
            _, reading = harness.readings[0]
            assert reading.aux_voltage_v == 3.815
        finally:
            harness.close()

    def test_reading_aux_none_without_meter(self, tmp_path):
        harness = Harness(tmp_path)
        try:
            harness.executor.submit(discharge_spec())
            harness.run(0.0, 3.0)
            _, reading = harness.readings[0]
            assert reading.aux_voltage_v is None
        finally:
            harness.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_job_executor.py::TestMeterCapture -v`
Expected: FAIL — `test_reading_carries_meter_voltage` gets `aux_voltage_v is None` (engine ignores the meter).

- [ ] **Step 3: Implement in `engine.py`**

In `_capture_reading`, replace the placeholder comment line:

```python
        # aux_voltage_v stays NULL until the meter driver lands (see spec)
```

with:

```python
        meter = self._registry.meter
        meter_status = meter.last_status if meter is not None else None
        aux_voltage_v = meter_status.voltage_v if meter_status is not None else None
```

and add `aux_voltage_v=aux_voltage_v,` to the `Reading(...)` construction (after the `runtime_s=...` argument).

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_job_executor.py -v`
Expected: all PASS (existing 13 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/engine.py tests/test_job_executor.py
git commit -m "Log meter voltage as aux_voltage_v on engine readings"
```

---

### Task 7: Facade cutoff-sourcing via the meter

**Files:**
- Modify: `load_test_bench/automation/test_runner.py`
- Test: `tests/test_test_runner_facade.py` (add cases)

**Interfaces:**
- Consumes: `profile_to_spec` and `TestRunner` (existing); `Phase._meter_voltage` already consumes a `"voltage_source": "meter"` param on discharge/timed/stepped phases.
- Produces (Task 10 sets it): `profile_to_spec(profile, battery_name, notes, voltage_source="device")` — when `voltage_source == "meter"`, the emitted discharge/timed/stepped phase params include `"voltage_source": "meter"` (rest phases are unaffected). `TestRunner.voltage_source: str = "device"` attribute; `TestRunner.start` passes `self.voltage_source` into `profile_to_spec`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_test_runner_facade.py`:

```python
class TestMeterCutoffSourcing:
    def test_profile_to_spec_defaults_to_device_voltage(self):
        from load_test_bench.automation.test_runner import profile_to_spec

        spec = profile_to_spec(DischargeProfile(name="d"), "", "")
        assert "voltage_source" not in spec.phases[0].params

    def test_meter_source_marks_discharge_phase(self):
        from load_test_bench.automation.test_runner import profile_to_spec

        spec = profile_to_spec(
            DischargeProfile(name="d"), "", "", voltage_source="meter"
        )
        assert spec.phases[0].params["voltage_source"] == "meter"

    def test_meter_source_marks_timed_and_stepped(self):
        from load_test_bench.automation.test_runner import profile_to_spec
        from load_test_bench.automation.profiles import SteppedProfile, TimedProfile

        timed = profile_to_spec(TimedProfile(name="t"), "", "", voltage_source="meter")
        assert timed.phases[0].params["voltage_source"] == "meter"
        stepped = profile_to_spec(
            SteppedProfile(name="s", steps=[{"current_a": 0.5, "duration_s": 10}]),
            "", "", voltage_source="meter",
        )
        assert stepped.phases[0].params["voltage_source"] == "meter"

    def test_cycle_rest_phases_are_not_marked(self):
        from load_test_bench.automation.test_runner import profile_to_spec
        from load_test_bench.automation.profiles import CycleProfile

        spec = profile_to_spec(
            CycleProfile(name="c", num_cycles=2), "", "", voltage_source="meter"
        )
        by_type = {p.phase_type: p for p in spec.phases}
        assert by_type["discharge"].params["voltage_source"] == "meter"
        assert "voltage_source" not in by_type["rest"].params

    def test_start_threads_runner_voltage_source(self, harness):
        """TestRunner.voltage_source flows into the submitted job's phases."""
        harness.runner.voltage_source = "meter"
        harness.runner.start(DischargeProfile(name="d", voltage_cutoff=3.0))
        harness.run(0.0, 1.0)
        job_id = harness.runner._job_id
        spec = harness.ledger.get_job(job_id)
        import json
        params = json.loads(spec["spec_json"])["phases"][0]["params"]
        assert params["voltage_source"] == "meter"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_test_runner_facade.py::TestMeterCutoffSourcing -v`
Expected: FAIL — `profile_to_spec() got an unexpected keyword argument 'voltage_source'`.

- [ ] **Step 3: Implement in `test_runner.py`**

Change the `profile_to_spec` signature and body. New signature:

```python
def profile_to_spec(
    profile: TestProfile, battery_name: str, notes: str, voltage_source: str = "device"
) -> JobSpec:
```

Inside, after each of the discharge, timed, and stepped `params = {...}` dictionaries are built (the three that create a voltage-sourced phase — NOT the cycle's rest, and NOT the cycle discharge inline dict), add the source. The cleanest approach: build all phases first as today, then post-process. Replace the final `return JobSpec(...)` block with a helper that stamps the source onto every non-rest phase:

```python
    if voltage_source == "meter":
        phases = tuple(
            PhaseSpec(p.phase_type, {**p.params, "voltage_source": "meter"})
            if p.phase_type != "rest"
            else p
            for p in phases
        )
    return JobSpec(
        name=profile.name,
        job_type=job_type,
        phases=phases,
        battery_name=battery_name,
        notes=notes,
    )
```

(This handles the CycleProfile discharge phases too, since they are non-rest, and leaves rest phases untouched.)

In `TestRunner.__init__`, add after `self.device = device`:

```python
        self.voltage_source = "device"  # "device" | "meter"; set by MainWindow
```

In `TestRunner.start`, change the `profile_to_spec` call to pass it:

```python
            spec = profile_to_spec(profile, battery_name, notes, self.voltage_source)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_test_runner_facade.py -v`
Expected: all PASS (existing 12 + 5 new).

Run: `uv run --extra dev pytest tests/test_phases.py tests/test_phase_cores.py -v`
Expected: still green (the meter seam in the cores/phases already handles `voltage_source: "meter"`).

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/automation/test_runner.py tests/test_test_runner_facade.py
git commit -m "Source discharge cutoff from the meter when enabled"
```

---

### Task 8: Meter settings persistence (`config.py`)

**Files:**
- Modify: `load_test_bench/config.py`
- Test: `tests/test_meter_config.py`

**Interfaces:**
- Consumes: nothing beyond stdlib `json` (already imported in config.py).
- Produces (Task 10 consumes): `MeterSettings` dataclass and pure `load_meter_settings(settings_path) -> MeterSettings` / `save_meter_settings(settings_path, settings) -> None`. Fields: `enabled: bool=False`, `transport: str="usb"`, `serial_port: str=""`, `host: str=""`, `lan_port: int=5555`, `profile_key: str="hds200"`, `use_for_cutoff: bool=False`. Load merges over defaults from the `"meter"` key of the JSON file; save reads-modifies-writes the whole file preserving other keys.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_meter_config.py`:

```python
"""Tests for meter settings load/save in settings.json."""

import json

from load_test_bench.config import (
    MeterSettings,
    load_meter_settings,
    save_meter_settings,
)


class TestMeterSettings:
    def test_defaults_when_file_absent(self, tmp_path):
        s = load_meter_settings(tmp_path / "settings.json")
        assert s == MeterSettings()
        assert s.enabled is False
        assert s.transport == "usb"
        assert s.profile_key == "hds200"

    def test_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        save_meter_settings(
            path,
            MeterSettings(
                enabled=True, transport="lan", host="10.0.0.9", lan_port=5555,
                profile_key="generic_scpi_dmm", use_for_cutoff=True,
            ),
        )
        loaded = load_meter_settings(path)
        assert loaded.enabled is True
        assert loaded.transport == "lan"
        assert loaded.host == "10.0.0.9"
        assert loaded.profile_key == "generic_scpi_dmm"
        assert loaded.use_for_cutoff is True

    def test_save_preserves_other_keys(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"notifications": {"ntfy_enabled": True}}))
        save_meter_settings(path, MeterSettings(enabled=True))
        data = json.loads(path.read_text())
        assert data["notifications"]["ntfy_enabled"] is True
        assert data["meter"]["enabled"] is True

    def test_partial_stored_merges_over_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"meter": {"enabled": True}}))
        loaded = load_meter_settings(path)
        assert loaded.enabled is True
        assert loaded.transport == "usb"  # default preserved
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_meter_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'MeterSettings' from 'load_test_bench.config'`

- [ ] **Step 3: Implement in `config.py`**

Add at the end of `load_test_bench/config.py` (ensure `from dataclasses import dataclass, asdict` and `import json` are imported at the top — add whichever is missing):

```python
@dataclass
class MeterSettings:
    """Optional SCPI voltage-meter connection settings."""

    enabled: bool = False
    transport: str = "usb"  # "usb" | "lan"
    serial_port: str = ""
    host: str = ""
    lan_port: int = 5555
    profile_key: str = "hds200"
    use_for_cutoff: bool = False


def load_meter_settings(settings_path) -> MeterSettings:
    """Read the 'meter' key of settings.json, merged over defaults."""
    defaults = MeterSettings()
    try:
        with open(settings_path) as f:
            stored = json.load(f).get("meter", {})
    except (OSError, json.JSONDecodeError):
        return defaults
    merged = {**asdict(defaults), **stored}
    # Only keep known fields so a stale/foreign key can't break construction.
    known = {f: merged[f] for f in asdict(defaults) if f in merged}
    return MeterSettings(**known)


def save_meter_settings(settings_path, settings: MeterSettings) -> None:
    """Write settings under the 'meter' key, preserving other top-level keys."""
    data = {}
    try:
        with open(settings_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data["meter"] = asdict(settings)
    try:
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_meter_config.py -v`
Expected: all PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/config.py tests/test_meter_config.py
git commit -m "Add meter connection settings persistence"
```

---

### Task 9: Voltage Monitor dialog (GUI)

**Files:**
- Create: `load_test_bench/gui/voltage_monitor_dialog.py`

**Interfaces:**
- Consumes: `MeterSettings`/`load_meter_settings`/`save_meter_settings` (Task 8); `METER_PROFILES` (Task 2); `list_serial_ports` (Task 1); `get_data_dir` from config.
- Produces (Task 10 consumes): `VoltageMonitorDialog(parent, meter, on_settings_changed)` where `meter` is the `ScpiMeter` instance and `on_settings_changed(settings: MeterSettings)` is a callback invoked after Save/connect changes so MainWindow can refresh the cutoff flag and status indicator. The dialog reads/writes `get_data_dir()/"settings.json"` via the Task-8 helpers and drives `meter.connect_usb`/`connect_lan`/`disconnect`.

No unit tests (GUI, per house style). Verification is an import check plus the Task-10 smoke test.

- [ ] **Step 1: Write the dialog**

Create `load_test_bench/gui/voltage_monitor_dialog.py`:

```python
"""Dialog to configure and connect the optional SCPI voltage meter.

The meter senses true battery-terminal voltage to mitigate cable IR drop.
Settings persist in settings.json (see config.MeterSettings). The dialog owns
no device state - it drives the shared ScpiMeter passed in by MainWindow.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_data_dir, load_meter_settings, save_meter_settings, MeterSettings
from ..protocol.meter_protocol import METER_PROFILES
from ..protocol.scpi_meter import MeterError
from ..protocol.scpi_transport import list_serial_ports


class VoltageMonitorDialog(QDialog):
    def __init__(self, parent, meter, on_settings_changed):
        super().__init__(parent)
        self.setWindowTitle("Voltage Monitor (SCPI Meter)")
        self._meter = meter
        self._on_settings_changed = on_settings_changed
        self._settings_file = get_data_dir() / "settings.json"

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Optional: sense true battery-terminal voltage with an SCPI meter "
            "to mitigate cable voltage drop. When enabled, the meter voltage is "
            "logged with every reading and (optionally) used for discharge cutoff."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.enabled_check = QCheckBox("Enable voltage meter")
        form.addRow(self.enabled_check)

        self.profile_combo = QComboBox()
        for key, profile in METER_PROFILES.items():
            self.profile_combo.addItem(profile.label, key)
        form.addRow("Instrument:", self.profile_combo)

        self.transport_combo = QComboBox()
        self.transport_combo.addItem("USB (serial)", "usb")
        self.transport_combo.addItem("LAN (TCP)", "lan")
        form.addRow("Transport:", self.transport_combo)

        self.serial_combo = QComboBox()
        self.serial_combo.setEditable(True)
        for device, description in list_serial_ports():
            self.serial_combo.addItem(f"{device} — {description}", device)
        self.serial_row = self._wrap_row("USB port:", self.serial_combo, form)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("10.0.0.9")
        self.host_row = self._wrap_row("Host:", self.host_edit, form)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5555)
        self.port_row = self._wrap_row("Port:", self.port_spin, form)

        self.cutoff_check = QCheckBox("Use meter voltage for discharge cutoff")
        form.addRow(self.cutoff_check)
        layout.addLayout(form)

        conn_row = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.status_label = QLabel("Not connected")
        conn_row.addWidget(self.connect_button)
        conn_row.addWidget(self.status_label, 1)
        layout.addLayout(conn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        layout.addWidget(buttons)

        self.transport_combo.currentIndexChanged.connect(self._update_transport_rows)
        self.connect_button.clicked.connect(self._on_connect_clicked)
        buttons.button(QDialogButtonBox.Save).clicked.connect(self._on_save)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.reject)

        self._load()
        self._update_transport_rows()
        self._refresh_connection_label()

    def _wrap_row(self, label, widget, form):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        form.addRow(label, container)
        return container

    def _update_transport_rows(self):
        is_usb = self.transport_combo.currentData() == "usb"
        self.serial_row.setVisible(is_usb)
        self.host_row.setVisible(not is_usb)
        self.port_row.setVisible(not is_usb)

    def _load(self):
        s = load_meter_settings(self._settings_file)
        self.enabled_check.setChecked(s.enabled)
        self._select_data(self.profile_combo, s.profile_key)
        self._select_data(self.transport_combo, s.transport)
        if s.serial_port:
            idx = self.serial_combo.findData(s.serial_port)
            if idx >= 0:
                self.serial_combo.setCurrentIndex(idx)
            else:
                self.serial_combo.setEditText(s.serial_port)
        self.host_edit.setText(s.host)
        self.port_spin.setValue(s.lan_port)
        self.cutoff_check.setChecked(s.use_for_cutoff)

    @staticmethod
    def _select_data(combo, value):
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _current_settings(self) -> MeterSettings:
        serial_port = self.serial_combo.currentData()
        if serial_port is None:
            serial_port = self.serial_combo.currentText().split(" — ")[0].strip()
        return MeterSettings(
            enabled=self.enabled_check.isChecked(),
            transport=self.transport_combo.currentData(),
            serial_port=serial_port,
            host=self.host_edit.text().strip(),
            lan_port=self.port_spin.value(),
            profile_key=self.profile_combo.currentData(),
            use_for_cutoff=self.cutoff_check.isChecked(),
        )

    def _on_save(self):
        settings = self._current_settings()
        save_meter_settings(self._settings_file, settings)
        self._on_settings_changed(settings)
        self.accept()

    def _on_connect_clicked(self):
        if self._meter.is_connected:
            self._meter.disconnect()
            self._refresh_connection_label()
            self._on_settings_changed(self._current_settings())
            return
        settings = self._current_settings()
        profile = METER_PROFILES[settings.profile_key]
        try:
            if settings.transport == "usb":
                if not settings.serial_port:
                    QMessageBox.warning(self, "Voltage Monitor", "Choose a USB port.")
                    return
                self._meter.connect_usb(settings.serial_port, profile)
            else:
                if not settings.host:
                    QMessageBox.warning(self, "Voltage Monitor", "Enter the meter host.")
                    return
                self._meter.connect_lan(settings.host, settings.lan_port, profile)
        except MeterError as e:
            QMessageBox.critical(self, "Voltage Monitor", str(e))
            return
        save_meter_settings(self._settings_file, settings)
        self._on_settings_changed(settings)
        self._refresh_connection_label()

    def _refresh_connection_label(self):
        if self._meter.is_connected:
            self.connect_button.setText("Disconnect")
            self.status_label.setText(f"Connected: {self._meter.identity}")
        else:
            self.connect_button.setText("Connect")
            self.status_label.setText("Not connected")
```

- [ ] **Step 2: Verify the module imports**

Run: `uv run --extra dev python -c "from load_test_bench.gui.voltage_monitor_dialog import VoltageMonitorDialog; print('ok')"`
Expected: prints `ok`

Run: `uv run --extra dev pytest`
Expected: still green (no test changes; import must not break collection).

- [ ] **Step 3: Commit**

```bash
git add load_test_bench/gui/voltage_monitor_dialog.py
git commit -m "Add Voltage Monitor configuration dialog"
```

---

### Task 10: MainWindow + charger-panel wiring

**Files:**
- Modify: `load_test_bench/gui/main_window.py`
- Modify: `load_test_bench/gui/dp832a_charger_panel.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces: running app with the meter wired — Device-menu "Voltage &Monitor…" action opens the dialog; a connected meter is registered in `device_registry` under `"meter"`; `test_runner.voltage_source` reflects enabled+use_for_cutoff+connected; the status bar shows the meter voltage; the DP832A panel shows a live meter readout; the meter is disconnected in `closeEvent`.

No unit tests (GUI). Verification is the full suite plus a headless smoke test.

- [ ] **Step 1: MainWindow imports and meter construction**

In `load_test_bench/gui/main_window.py`, add imports (near the other jobs/protocol imports):

```python
from ..protocol.scpi_meter import ScpiMeter
from ..config import load_meter_settings, save_meter_settings
from .voltage_monitor_dialog import VoltageMonitorDialog
```

Add a class-level signal alongside the others (e.g. after `recovery_safe_result = Signal(str)`):

```python
    meter_status_updated = Signal(object)  # MeterStatus, marshalled to the GUI thread
```

In `__init__`, directly after the `self.device_registry = DeviceRegistry()` line, add:

```python
        # Optional SCPI voltage meter (cable-drop mitigation). Read-only; not
        # wired into the safety supervisor. Registered in device_registry when
        # connected so the engine logs its voltage and can source cutoff from it.
        self.meter = ScpiMeter()
        self.meter.set_status_callback(self.meter_status_updated.emit)
        self.meter_status_updated.connect(self._on_meter_status)
```

- [ ] **Step 2: Device-menu action**

In `_create_menus`, in the Device menu block (after the `debug_action` is added), add:

```python
        device_menu.addSeparator()

        voltage_monitor_action = QAction("Voltage &Monitor…", self)
        voltage_monitor_action.setMenuRole(QAction.NoRole)
        voltage_monitor_action.triggered.connect(self._show_voltage_monitor)
        device_menu.addAction(voltage_monitor_action)
```

- [ ] **Step 3: Status-bar indicator and the meter methods**

After the safety banner is added to the statusbar (in the engine-wiring block that adds `self.safety_banner`), add:

```python
        self.meter_label = QLabel("")
        self.meter_label.setToolTip("SCPI voltage meter (battery-terminal sense)")
        self.meter_label.hide()
        self.statusbar.addPermanentWidget(self.meter_label)
```

Add these methods to `MainWindow`:

```python
    def _show_voltage_monitor(self) -> None:
        dialog = VoltageMonitorDialog(self, self.meter, self._on_meter_settings_changed)
        dialog.exec()
        # Reflect any connect/disconnect the dialog performed.
        self._apply_meter_registration()

    def _on_meter_settings_changed(self, settings) -> None:
        self._apply_meter_registration()

    def _apply_meter_registration(self) -> None:
        """Register/deregister the meter and set the cutoff source to match."""
        settings = load_meter_settings(get_data_dir() / "settings.json")
        if self.meter.is_connected:
            self.device_registry.register("meter", self.meter)
        else:
            self.device_registry.unregister("meter")
        use_meter = (
            settings.enabled and settings.use_for_cutoff and self.meter.is_connected
        )
        if self.test_runner is not None:
            self.test_runner.voltage_source = "meter" if use_meter else "device"
        self.meter_label.setVisible(self.meter.is_connected)
        if not self.meter.is_connected:
            self.meter_label.setText("")

    @Slot(object)
    def _on_meter_status(self, status) -> None:
        self.meter_label.setText(f"Meter: {status.voltage_v:.3f} V")
        self.meter_label.show()
        # Forward to the charger panel's live readout.
        self.dp832a_charger_panel.set_meter_voltage(status.voltage_v)
```

Auto-connect at startup if enabled: at the very end of `__init__` (after the engine/facade block), add:

```python
        self._maybe_autoconnect_meter()
```

and the method:

```python
    def _maybe_autoconnect_meter(self) -> None:
        settings = load_meter_settings(get_data_dir() / "settings.json")
        if not settings.enabled:
            return
        from ..protocol.meter_protocol import METER_PROFILES
        from ..protocol.scpi_meter import MeterError

        profile = METER_PROFILES.get(settings.profile_key)
        if profile is None:
            return
        try:
            if settings.transport == "usb" and settings.serial_port:
                self.meter.connect_usb(settings.serial_port, profile)
            elif settings.transport == "lan" and settings.host:
                self.meter.connect_lan(settings.host, settings.lan_port, profile)
        except MeterError:
            return  # meter absent at startup - user can retry via the dialog
        self._apply_meter_registration()
```

- [ ] **Step 4: closeEvent shutdown**

In `closeEvent`, directly before `self.dp832a_charger_panel.shutdown()`, add:

```python
        self.meter.disconnect()
```

- [ ] **Step 5: DP832A panel live meter readout**

In `load_test_bench/gui/dp832a_charger_panel.py`, add a meter row to the "Charge Status" grid. That grid's existing rows are Voltage (row 0), Current (1), Power (2), Mode (3), Elapsed (4), and the state label spanning row 5, so row 6 is the next free row. In `_create_ui`, immediately after the `status_layout.addWidget(self.state_label, 5, 0, 1, 2)` line, add:

```python
        status_layout.addWidget(QLabel("Battery (meter):"), 6, 0)
        self.meter_voltage_label = QLabel("--")
        status_layout.addWidget(self.meter_voltage_label, 6, 1)
```

If reading the file shows a different last-row index, use the row immediately after the state label instead of a hard-coded 6.

Add the method:

```python
    def set_meter_voltage(self, voltage_v: float) -> None:
        """Show the independent meter's battery-terminal voltage (from MainWindow)."""
        self.meter_voltage_label.setText(f"{voltage_v:.3f} V")
```

- [ ] **Step 6: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: all PASS (still the Task-8 total; GUI wiring adds no tests).

- [ ] **Step 7: Smoke test (headless)**

Kill any prior app instance you launched (by task ID), then launch non-blocking: `uv run python -m load_test_bench.main`. Wait ~15 s. Verify from the output: no traceback. Interactive checks (opening the Voltage Monitor dialog, a real meter connect, the status-bar readout) are deferred to the user — note that in the report. Kill the instance.

Also verify the panel import: `uv run --extra dev python -c "from load_test_bench.gui.dp832a_charger_panel import DP832AChargerPanel; print('ok')"`.

- [ ] **Step 8: Commit**

```bash
git add load_test_bench/gui/main_window.py load_test_bench/gui/dp832a_charger_panel.py
git commit -m "Wire SCPI voltage meter into the app with a status readout"
```

---

### Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md`, `CHANGELOG.md`, `TODO.md`, `README.md`

**Interfaces:** none — docs only.

- [ ] **Step 1: CLAUDE.md**

After the "### Rigol DP832A Charger (LAN)" section (or the "### Job Engine" section, whichever the meter fits after), add:

```markdown
### SCPI Voltage Meter (cable-drop mitigation)

Optional. An SCPI DMM (OWON HDS200 over USB by default, or any SCPI voltmeter
over USB/LAN) senses true battery-terminal voltage to mitigate cable IR drop
measured at the load/PSU.

- `protocol/scpi_transport.py` - `UsbScpiLink` (pyserial CDC) joins `LanScpiLink`
  under the same `ScpiTransport`; `list_serial_ports()` for the UI
- `protocol/meter_protocol.py` - `MeterProfile` + `METER_PROFILES` (built-ins:
  `hds200` DMM subsystem, `generic_scpi_dmm` standard `MEAS:VOLT:DC?`);
  `parse_measurement`, `make_idn_verifier`. Add an instrument = add a profile.
- `protocol/scpi_meter.py` - `ScpiMeter` (duck-types the `MeterDevice` role);
  read-only, so NOT wired into the SafetySupervisor
- Engine: `_capture_reading` logs the registered meter's voltage as
  `readings.aux_voltage_v` (column from migration 1); exported in CSV/JSON/Excel
- Cutoff sourcing: when the meter is enabled + "use for cutoff" + connected,
  `TestRunner.voltage_source = "meter"` stamps `voltage_source: "meter"` on
  discharge/timed/stepped phase params; `Phase._meter_voltage` then overrides the
  load's own reading. Falls back to the load voltage (conservative early cutoff)
  if the meter drops out.
- GUI: Device → "Voltage Monitor…" (`voltage_monitor_dialog.py`) configures and
  connects; settings persist under the `meter` key of settings.json
  (`config.MeterSettings`); a status-bar readout and the charger panel show live
  battery-terminal voltage
- Scope: cutoff sourcing covers the engine/facade discharge path (Battery
  Capacity, Power Bank). The pre-engine panel sweeps and the manual-logging path
  are future work; closed-loop charge compensation is out of scope.
```

Update the "Test Coverage" section: change the total and file count to whatever `uv run --extra dev pytest --collect-only -q 2>/dev/null | tail -1` reports, and add lines:

```markdown
- `test_usb_scpi_link.py` (6) - USB CDC SCPI link
- `test_meter_protocol.py` (11) - meter profiles + measurement parsing
- `test_scpi_meter.py` (6) - profile-driven SCPI meter driver
- `test_aux_voltage_persistence.py` (5) - aux voltage in the readings table
- `test_meter_config.py` (4) - meter settings persistence
```

(Adjust each count to the actual per-file collected number; the `test_export.py`, `test_job_executor.py`, and `test_test_runner_facade.py` counts also grew — update those existing lines too.)

- [ ] **Step 2: CHANGELOG.md**

Under `## [Unreleased]` → `### Added`, add:

```markdown
- **Optional SCPI voltage meter** — sense true battery-terminal voltage with an
  OWON HDS200 (USB) or any SCPI DMM (USB or LAN) to mitigate cable voltage drop.
  The meter voltage is logged with every reading (`aux_voltage_v`, exported in
  CSV/JSON/Excel) and can override the load-measured voltage for discharge
  cutoff. Configured via Device → Voltage Monitor; instruments are added as
  profiles.
```

- [ ] **Step 3: TODO.md**

Under "### Job Engine follow-ups" (or a new "### Voltage Meter follow-ups"), add:

```markdown
### Voltage Meter follow-ups
- Meter cutoff/logging for the pre-engine panel sweeps (Battery Load, Charger
  Load) — arrives when those panels migrate to the job engine
- Meter aux-voltage logging on the manual DL24 control-panel logging path
- Custom instrument-profile authoring UI (currently profiles are code-defined)
- Closed-loop charge-voltage compensation against the meter (PSU setpoint trim)
```

- [ ] **Step 4: README.md**

In the Requirements section (near the optional DP832A hardware line), add:

```markdown
- **Optional hardware:** SCPI voltage meter for cable-drop mitigation — OWON
  HDS200 (USB) or any SCPI DMM (USB or LAN)
```

In the feature list under Test Bench, add a bullet:

```markdown
- **Voltage Monitor** - Optional SCPI meter senses true battery-terminal voltage (mitigates cable drop); logs `aux_voltage_v` and can drive discharge cutoff
```

- [ ] **Step 5: Run the full suite once more and commit**

Run: `uv run --extra dev pytest`
Expected: all green.

```bash
git add CLAUDE.md CHANGELOG.md TODO.md README.md
git commit -m "Document the optional SCPI voltage meter"
```

---

## Execution Notes

- Tasks run in order (each consumes prior interfaces). Tasks 1–8 are pure/unit-tested; 9–10 are GUI (import + smoke only); 11 is docs.
- After Task 11, the feature is complete: an optional meter that logs true battery voltage everywhere the engine runs and sources discharge cutoff when enabled.
- The meter is read-only and deliberately kept out of the SafetySupervisor — do not add it there.
- Intentional scope boundaries (state in the final report, do not silently exceed): pre-engine panel sweeps, the manual-logging path, custom-profile UI, and closed-loop charge compensation are all future work.
