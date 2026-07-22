# Rigol DP832A LAN Charger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Battery Charging" tab that charges a battery using a Rigol DP832A bench power supply controlled over its LAN interface (raw SCPI on TCP port 5555), with CC-CV charge termination (current taper) and a safety timeout.

**Architecture:** Three new layers mirroring the existing DL24 stack: a pure SCPI string builder/parser (`dp832a_protocol.py`, testable with no I/O, like `px100_protocol.py`), a socket transport with a 1 Hz polling thread and GUI lock timeout (`rigol_dp832a.py`, mirroring `USBHIDDevice`'s structure), and a self-contained GUI panel that owns the charger device (like `charger_panel.py` owns its test flow). A pure `ChargeMonitor` state machine decides when charging is done. `MainWindow` only adds the tab and shuts the panel down on close — the single-device DL24 machinery is untouched.

**Tech Stack:** Python ≥3.10 stdlib `socket` + `threading` (no new dependencies), PySide6, pytest.

**Background — DP832A facts the implementer needs:**
- LAN control: raw SCPI over TCP, port **5555**. Commands and responses are ASCII lines terminated with `\n`.
- `*IDN?` returns e.g. `RIGOL TECHNOLOGIES,DP832A,DP8A123456789,00.01.16`.
- 3 channels: CH1 and CH2 are 30 V / 3 A, CH3 is 5 V / 3 A (setpoint overrange to 32 V / 3.2 A / 5.3 V is accepted by the instrument).
- Key commands: `:SOUR<n>:VOLT <v>`, `:SOUR<n>:CURR <a>`, `:OUTP CH<n>,ON|OFF`, `:OUTP? CH<n>` → `ON`/`OFF`, `:MEAS:ALL? CH<n>` → `volts,amps,watts`, `:OUTP:MODE? CH<n>` → `CC`/`CV`/`UR`, `:OUTP:OVP:VAL CH<n>,<v>`, `:OUTP:OVP CH<n>,ON|OFF`.
- A bench PSU with a voltage limit + current limit *is* a CC-CV charger: it charges in CC (constant current) until the battery reaches the voltage setpoint, then the mode flips to CV and current tapers. Charge is "done" when taper current falls below a cutoff (e.g. C/20).

**Scope decisions (assumptions made — flag to the user if wrong):**
- Manual charge control panel only; no charge-curve plotting, no database logging, no automated charge→discharge cycling in this version.
- Charger connection settings (IP, port, channel) and charge parameters persist in the panel's session file, per the Test Automation panel persistence rule in CLAUDE.md.
- OVP is set automatically to (charge voltage + 0.1 V) and enabled at charge start — a cheap safety net for battery charging.

## Global Constraints

- Python `>=3.10` (from `pyproject.toml`); run everything via `uv` (`uv run --extra dev pytest`).
- **No new dependencies** — SCPI over stdlib `socket` only; do not add pyvisa.
- Device status callbacks run in a background thread — GUI updates MUST go through a Qt `Signal` (CLAUDE.md "Qt Threading Safety").
- GUI-initiated device commands MUST use a 1-second lock timeout (`GUI_LOCK_TIMEOUT = 1.0`) so a slow network never freezes the UI (CLAUDE.md "Lock Timeout for GUI Operations").
- Test Automation panels MUST persist UI state to `sessions/<name>_session.json` with `_save_session()` / `_load_session()` / `_connect_save_signals()` and a `_loading_settings` guard flag (CLAUDE.md "Test Automation Panel State Persistence").
- Protocol tests are pure (bytes/strings in, values out) with no transport, matching `tests/test_px100_protocol.py` style: pytest, `TestXxx` classes, `test_<behavior>` methods with docstrings, no `unittest.mock`.
- Commit messages follow existing repo style: imperative sentence, no `feat:` prefix (e.g. "Add DL24P power connector specs to README").

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `load_test_bench/protocol/dp832a_protocol.py` | Create | `ChargerStatus` dataclass, `CHANNEL_LIMITS`, `DP832AProtocol` — pure SCPI string build/parse, no I/O |
| `load_test_bench/protocol/rigol_dp832a.py` | Create | `RigolDP832A` — TCP transport, poll thread, thread-safe commands, callbacks; `ChargerError` |
| `load_test_bench/automation/charge_monitor.py` | Create | `ChargeMonitor` + `ChargeState` — pure CC-CV termination state machine |
| `load_test_bench/gui/dp832a_charger_panel.py` | Create | `DP832AChargerPanel` — connection UI, charge settings, live readout, start/stop, session persistence |
| `load_test_bench/gui/main_window.py` | Modify | Import panel, add "Battery Charging" tab (~line 477), shut panel down in `closeEvent` (~line 4199) |
| `CLAUDE.md` | Modify | Document the new charger architecture |
| `tests/test_dp832a_protocol.py` | Create | Protocol build/parse tests |
| `tests/test_rigol_dp832a.py` | Create | Driver tests against a scripted `FakeSocket` |
| `tests/test_charge_monitor.py` | Create | Termination state machine tests |

---

### Task 1: DP832A SCPI Protocol (pure build/parse)

**Files:**
- Create: `load_test_bench/protocol/dp832a_protocol.py`
- Test: `tests/test_dp832a_protocol.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces (used by Tasks 2–4):
  - `ChargerStatus` dataclass: fields `voltage_v: float, current_a: float, power_w: float, output_on: bool, mode: str, channel: int` (mode is `"CC"`, `"CV"`, or `"UR"`).
  - `CHANNEL_LIMITS: dict[int, tuple[float, float]]` mapping channel → `(max_voltage, max_current)`.
  - `DP832AProtocol` with classmethods/staticmethods: `cmd_idn() -> str`, `cmd_set_voltage(channel, volts) -> str`, `cmd_set_current(channel, amps) -> str`, `cmd_set_output(channel, on) -> str`, `cmd_query_output(channel) -> str`, `cmd_measure_all(channel) -> str`, `cmd_query_mode(channel) -> str`, `cmd_set_ovp_value(channel, volts) -> str`, `cmd_set_ovp_state(channel, on) -> str`, `parse_idn(response) -> bool`, `parse_measure_all(response) -> tuple[float, float, float]`, `parse_output_state(response) -> bool`, `parse_mode(response) -> str`. Builders raise `ValueError` on bad channel or out-of-range setpoint; parsers raise `ValueError` on malformed responses.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dp832a_protocol.py`:

```python
"""Tests for the Rigol DP832A SCPI protocol builder/parser."""

import pytest

from load_test_bench.protocol.dp832a_protocol import (
    CHANNEL_LIMITS,
    ChargerStatus,
    DP832AProtocol,
)


class TestCommandBuilding:
    def test_idn_query(self):
        """Identification query is the bare SCPI standard command."""
        assert DP832AProtocol.cmd_idn() == "*IDN?"

    def test_set_voltage_ch1(self):
        """Voltage setpoint uses the SOUR<n> form with 3 decimals."""
        assert DP832AProtocol.cmd_set_voltage(1, 4.2) == ":SOUR1:VOLT 4.200"

    def test_set_voltage_rejects_over_range(self):
        """CH3 tops out at 5.3 V - higher setpoints are refused."""
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_voltage(3, 12.0)

    def test_set_voltage_rejects_bad_channel(self):
        """The DP832A only has channels 1-3."""
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_voltage(4, 1.0)

    def test_set_current(self):
        assert DP832AProtocol.cmd_set_current(2, 0.5) == ":SOUR2:CURR 0.500"

    def test_set_current_rejects_over_range(self):
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_current(1, 5.0)

    def test_output_on_off(self):
        assert DP832AProtocol.cmd_set_output(1, True) == ":OUTP CH1,ON"
        assert DP832AProtocol.cmd_set_output(3, False) == ":OUTP CH3,OFF"

    def test_queries(self):
        assert DP832AProtocol.cmd_query_output(1) == ":OUTP? CH1"
        assert DP832AProtocol.cmd_measure_all(2) == ":MEAS:ALL? CH2"
        assert DP832AProtocol.cmd_query_mode(1) == ":OUTP:MODE? CH1"

    def test_ovp_commands(self):
        assert DP832AProtocol.cmd_set_ovp_value(1, 4.3) == ":OUTP:OVP:VAL CH1,4.300"
        assert DP832AProtocol.cmd_set_ovp_state(1, True) == ":OUTP:OVP CH1,ON"

    def test_channel_limits_cover_all_channels(self):
        """CH1/CH2 are the 30 V channels, CH3 the 5 V channel."""
        assert CHANNEL_LIMITS[1] == (32.0, 3.2)
        assert CHANNEL_LIMITS[2] == (32.0, 3.2)
        assert CHANNEL_LIMITS[3] == (5.3, 3.2)


class TestResponseParsing:
    def test_parse_idn_accepts_dp832(self):
        """Real instrument IDN string (with trailing newline) is accepted."""
        idn = "RIGOL TECHNOLOGIES,DP832A,DP8A123456789,00.01.16\n"
        assert DP832AProtocol.parse_idn(idn) is True

    def test_parse_idn_rejects_other_instruments(self):
        assert DP832AProtocol.parse_idn("RIGOL TECHNOLOGIES,DS1054Z,X,Y") is False
        assert DP832AProtocol.parse_idn("garbage") is False

    def test_parse_measure_all(self):
        """':MEAS:ALL?' returns 'volts,amps,watts'."""
        assert DP832AProtocol.parse_measure_all("4.105,0.512,2.102\n") == (
            4.105,
            0.512,
            2.102,
        )

    def test_parse_measure_all_rejects_garbage(self):
        with pytest.raises(ValueError):
            DP832AProtocol.parse_measure_all("ERR")

    def test_parse_output_state(self):
        assert DP832AProtocol.parse_output_state("ON\n") is True
        assert DP832AProtocol.parse_output_state("OFF\n") is False

    def test_parse_mode(self):
        assert DP832AProtocol.parse_mode("CV\n") == "CV"
        assert DP832AProtocol.parse_mode("CC\n") == "CC"
        with pytest.raises(ValueError):
            DP832AProtocol.parse_mode("??")


class TestChargerStatus:
    def test_fields(self):
        """ChargerStatus carries one channel's live snapshot."""
        status = ChargerStatus(
            voltage_v=4.1,
            current_a=0.5,
            power_w=2.05,
            output_on=True,
            mode="CC",
            channel=1,
        )
        assert status.voltage_v == 4.1
        assert status.output_on is True
        assert status.channel == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_dp832a_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.protocol.dp832a_protocol'`

- [ ] **Step 3: Write the implementation**

Create `load_test_bench/protocol/dp832a_protocol.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_dp832a_protocol.py -v`
Expected: all PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/protocol/dp832a_protocol.py tests/test_dp832a_protocol.py
git commit -m "Add Rigol DP832A SCPI protocol builder and parser"
```

---

### Task 2: RigolDP832A LAN Driver (socket transport + poll thread)

**Files:**
- Create: `load_test_bench/protocol/rigol_dp832a.py`
- Test: `tests/test_rigol_dp832a.py`

**Interfaces:**
- Consumes (Task 1): `DP832AProtocol`, `ChargerStatus`, `CHANNEL_LIMITS`.
- Produces (used by Task 4):
  - `ChargerError(Exception)`.
  - `RigolDP832A` with: `connect(host: str, port: int = 5555) -> bool` (raises `ChargerError`), `disconnect() -> None`, `set_channel(channel: int) -> None`, `set_voltage(volts: float) -> bool`, `set_current(amps: float) -> bool`, `set_ovp(volts: float) -> bool`, `output_on() -> bool`, `output_off() -> bool`, `set_status_callback(cb: Callable[[ChargerStatus], None])`, `set_error_callback(cb: Callable[[str], None])`; properties `is_connected: bool`, `host: Optional[str]`, `identity: str`, `channel: int`, `last_status: Optional[ChargerStatus]`. Class constants `DEFAULT_PORT = 5555`, `POLL_INTERVAL = 1.0`, `SOCKET_TIMEOUT = 2.0`, `GUI_LOCK_TIMEOUT = 1.0`.
  - Commands return `False` (and fire the error callback) on I/O failure or lock timeout — they never raise from I/O errors. The status callback fires on the poll thread; GUI consumers must re-emit via a Qt Signal.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rigol_dp832a.py`. Tests inject a scripted fake socket directly (no network, no mock library), matching the repo's no-mock testing style:

```python
"""Tests for the RigolDP832A LAN driver using a scripted fake socket."""

from load_test_bench.protocol.rigol_dp832a import RigolDP832A


class FakeSocket:
    """Stand-in for a TCP socket speaking DP832A SCPI.

    Records every command sent; queues a canned response for queries.
    """

    def __init__(self, responses=None):
        self.responses = {
            "*IDN?": "RIGOL TECHNOLOGIES,DP832A,DP8A123456789,00.01.16\n",
            ":MEAS:ALL? CH1": "4.105,0.512,2.102\n",
            ":OUTP? CH1": "ON\n",
            ":OUTP:MODE? CH1": "CC\n",
        }
        if responses:
            self.responses.update(responses)
        self.sent = []
        self._pending = b""

    def sendall(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, n):
        data, self._pending = self._pending, b""
        return data

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


class BrokenSocket(FakeSocket):
    """Socket whose writes always fail."""

    def sendall(self, data):
        raise OSError("network unreachable")


def make_device(sock=None):
    """Device wired to a fake socket, bypassing connect() (no poll thread)."""
    device = RigolDP832A()
    device._sock = sock if sock is not None else FakeSocket()
    device._connected = True
    return device


class TestCommands:
    def test_set_voltage_sends_scpi(self):
        device = make_device()
        assert device.set_voltage(4.2) is True
        assert device._sock.sent == [":SOUR1:VOLT 4.200"]

    def test_channel_selection_changes_commands(self):
        """Commands target whichever channel was selected."""
        device = make_device()
        device.set_channel(2)
        device.set_current(1.5)
        assert device._sock.sent == [":SOUR2:CURR 1.500"]

    def test_output_on_off(self):
        device = make_device()
        device.output_on()
        device.output_off()
        assert device._sock.sent == [":OUTP CH1,ON", ":OUTP CH1,OFF"]

    def test_set_ovp_sends_value_then_enable(self):
        device = make_device()
        assert device.set_ovp(4.3) is True
        assert device._sock.sent == [":OUTP:OVP:VAL CH1,4.300", ":OUTP:OVP CH1,ON"]

    def test_command_failure_returns_false_and_reports(self):
        """I/O errors surface via the error callback, never as exceptions."""
        device = make_device(BrokenSocket())
        errors = []
        device.set_error_callback(errors.append)
        assert device.set_voltage(4.2) is False
        assert len(errors) == 1


class TestPolling:
    def test_poll_once_builds_status(self):
        """One poll pass reads V/I/P, output state, and regulation mode."""
        device = make_device()
        status = device._poll_once()
        assert status.voltage_v == 4.105
        assert status.current_a == 0.512
        assert status.power_w == 2.102
        assert status.output_on is True
        assert status.mode == "CC"
        assert status.channel == 1

    def test_poll_once_output_off_reports_ur_without_mode_query(self):
        """With the output off the mode query is skipped; mode reads UR."""
        device = make_device(FakeSocket({":OUTP? CH1": "OFF\n"}))
        status = device._poll_once()
        assert status.output_on is False
        assert status.mode == "UR"
        assert ":OUTP:MODE? CH1" not in device._sock.sent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_rigol_dp832a.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.protocol.rigol_dp832a'`

- [ ] **Step 3: Write the implementation**

Create `load_test_bench/protocol/rigol_dp832a.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_rigol_dp832a.py -v`
Expected: all PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/protocol/rigol_dp832a.py tests/test_rigol_dp832a.py
git commit -m "Add Rigol DP832A LAN driver with polling thread"
```

---

### Task 3: Charge Termination Monitor (pure state machine)

**Files:**
- Create: `load_test_bench/automation/charge_monitor.py`
- Test: `tests/test_charge_monitor.py`

**Interfaces:**
- Consumes (Task 1): `ChargerStatus`.
- Produces (used by Task 4):
  - `ChargeState(Enum)`: `IDLE`, `CHARGING`, `COMPLETE`, `TIMED_OUT`, `FAULT`.
  - `ChargeMonitor(termination_current_a: float, timeout_s: float, taper_samples: int = 5)` with `start(now_s: float) -> None`, `update(status: ChargerStatus, now_s: float) -> ChargeState`, `elapsed_s(now_s: float) -> float`, attribute `state: ChargeState`. Timestamps are caller-supplied monotonic seconds (`time.monotonic()`) so tests are deterministic.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_charge_monitor.py`:

```python
"""Tests for the CC-CV charge termination state machine."""

from load_test_bench.automation.charge_monitor import ChargeMonitor, ChargeState
from load_test_bench.protocol.dp832a_protocol import ChargerStatus


def make_status(current_a=1.0, mode="CC", output_on=True):
    return ChargerStatus(
        voltage_v=4.2,
        current_a=current_a,
        power_w=4.2 * current_a,
        output_on=output_on,
        mode=mode,
        channel=1,
    )


class TestChargeMonitor:
    def test_starts_idle(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        assert monitor.state == ChargeState.IDLE
        assert monitor.elapsed_s(100.0) == 0.0

    def test_charging_after_start(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        monitor.start(now_s=100.0)
        assert monitor.state == ChargeState.CHARGING
        assert monitor.update(make_status(current_a=1.0), now_s=101.0) == ChargeState.CHARGING
        assert monitor.elapsed_s(101.0) == 1.0

    def test_completes_after_consecutive_taper_samples(self):
        """CV mode with current at/below cutoff for taper_samples ticks ends charge."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=5)
        monitor.start(now_s=0.0)
        for tick in range(4):
            state = monitor.update(make_status(current_a=0.04, mode="CV"), now_s=tick + 1)
            assert state == ChargeState.CHARGING
        assert monitor.update(make_status(current_a=0.04, mode="CV"), now_s=5.0) == ChargeState.COMPLETE

    def test_taper_count_resets_on_current_blip(self):
        """A single high-current sample restarts the taper count."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=3)
        monitor.start(now_s=0.0)
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=1.0)
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=2.0)
        monitor.update(make_status(current_a=0.50, mode="CV"), now_s=3.0)  # blip
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=4.0)
        assert monitor.update(make_status(current_a=0.04, mode="CV"), now_s=5.0) == ChargeState.CHARGING

    def test_low_current_in_cc_mode_does_not_complete(self):
        """Taper only counts in CV mode - CC means the battery is still pulling."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=2)
        monitor.start(now_s=0.0)
        for tick in range(5):
            state = monitor.update(make_status(current_a=0.01, mode="CC"), now_s=tick + 1)
        assert state == ChargeState.CHARGING

    def test_safety_timeout(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=100.0)
        monitor.start(now_s=0.0)
        assert monitor.update(make_status(), now_s=99.0) == ChargeState.CHARGING
        assert monitor.update(make_status(), now_s=100.0) == ChargeState.TIMED_OUT

    def test_output_off_is_fault(self):
        """If the output drops (OVP trip, front-panel off), flag a fault."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        monitor.start(now_s=0.0)
        assert monitor.update(make_status(output_on=False), now_s=1.0) == ChargeState.FAULT

    def test_terminal_states_are_sticky(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=100.0)
        monitor.start(now_s=0.0)
        monitor.update(make_status(), now_s=100.0)
        assert monitor.update(make_status(current_a=0.01, mode="CV"), now_s=101.0) == ChargeState.TIMED_OUT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_charge_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.automation.charge_monitor'`

- [ ] **Step 3: Write the implementation**

Create `load_test_bench/automation/charge_monitor.py`:

```python
"""Charge termination state machine for PSU-based CC-CV battery charging.

Pure logic - no I/O, no Qt. The GUI panel feeds it one ChargerStatus per poll
tick plus a monotonic timestamp, and acts on the returned state. Timestamps
are caller-supplied so tests are deterministic.
"""

from enum import Enum

from ..protocol.dp832a_protocol import ChargerStatus


class ChargeState(Enum):
    IDLE = "idle"
    CHARGING = "charging"
    COMPLETE = "complete"
    TIMED_OUT = "timed_out"
    FAULT = "fault"


class ChargeMonitor:
    """Decides when a CC-CV charge is finished.

    Complete when the supply is in CV mode and current has stayed at or below
    termination_current_a for taper_samples consecutive updates. Taper is only
    counted in CV mode: in CC mode the battery is still drawing full current,
    and brief low-current readings there (e.g. during connection) must not
    end the charge.
    """

    def __init__(
        self,
        termination_current_a: float,
        timeout_s: float,
        taper_samples: int = 5,
    ) -> None:
        self.termination_current_a = termination_current_a
        self.timeout_s = timeout_s
        self.taper_samples = taper_samples
        self.state = ChargeState.IDLE
        self._started_at = 0.0
        self._taper_count = 0

    def start(self, now_s: float) -> None:
        self.state = ChargeState.CHARGING
        self._started_at = now_s
        self._taper_count = 0

    def elapsed_s(self, now_s: float) -> float:
        if self.state == ChargeState.IDLE:
            return 0.0
        return now_s - self._started_at

    def update(self, status: ChargerStatus, now_s: float) -> ChargeState:
        if self.state != ChargeState.CHARGING:
            return self.state
        if not status.output_on:
            self.state = ChargeState.FAULT
        elif now_s - self._started_at >= self.timeout_s:
            self.state = ChargeState.TIMED_OUT
        elif status.mode == "CV" and status.current_a <= self.termination_current_a:
            self._taper_count += 1
            if self._taper_count >= self.taper_samples:
                self.state = ChargeState.COMPLETE
        else:
            self._taper_count = 0
        return self.state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_charge_monitor.py -v`
Expected: all PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/automation/charge_monitor.py tests/test_charge_monitor.py
git commit -m "Add CC-CV charge termination monitor"
```

---

### Task 4: DP832A Charger Panel (GUI)

**Files:**
- Create: `load_test_bench/gui/dp832a_charger_panel.py`

**Interfaces:**
- Consumes: `RigolDP832A`, `ChargerError` (Task 2); `ChargerStatus`, `CHANNEL_LIMITS` (Task 1); `ChargeMonitor`, `ChargeState` (Task 3); `get_data_dir` from `load_test_bench.config`.
- Produces (used by Task 5): `DP832AChargerPanel(QWidget)` with a no-argument-beyond-parent constructor `DP832AChargerPanel(parent=None)` and a `shutdown() -> None` method (stops charging, turns output off, disconnects) for `MainWindow.closeEvent`.

There are no GUI unit tests in this repo (no pytest-qt tests exist); verification is an import check here and an app smoke test in Task 5.

- [ ] **Step 1: Write the panel**

Create `load_test_bench/gui/dp832a_charger_panel.py`:

```python
"""Battery charging panel driving a Rigol DP832A power supply over LAN.

Self-contained: the panel owns its RigolDP832A instance (panels in this app
drive their devices directly - see charger_panel.py). Status callbacks arrive
on the poll thread and are marshalled to the GUI thread via Qt signals.
"""

import json
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ..automation.charge_monitor import ChargeMonitor, ChargeState
from ..config import get_data_dir
from ..protocol.dp832a_protocol import CHANNEL_LIMITS
from ..protocol.rigol_dp832a import ChargerError, RigolDP832A

OVP_MARGIN_V = 0.1  # OVP armed this far above the charge voltage


class DP832AChargerPanel(QWidget):
    # Marshal poll-thread callbacks onto the GUI thread
    charger_status = Signal(object)  # ChargerStatus
    charger_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.charger = RigolDP832A()
        self.charger.set_status_callback(self.charger_status.emit)
        self.charger.set_error_callback(self.charger_error.emit)

        self._monitor: Optional[ChargeMonitor] = None
        self._loading_settings = False
        self._session_file = get_data_dir() / "sessions" / "dp832a_charger_session.json"

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        self._create_ui()
        self._load_session()
        self._connect_save_signals()

        self.charger_status.connect(self._on_charger_status)
        self.charger_error.connect(self._on_charger_error)

    # --- UI construction ---

    def _create_ui(self) -> None:
        layout = QHBoxLayout(self)

        # Connection
        conn_group = QGroupBox("DP832A Connection (LAN)")
        conn_layout = QGridLayout(conn_group)
        conn_layout.addWidget(QLabel("IP Address:"), 0, 0)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("192.168.1.100")
        conn_layout.addWidget(self.host_edit, 0, 1)
        conn_layout.addWidget(QLabel("Port:"), 1, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(RigolDP832A.DEFAULT_PORT)
        conn_layout.addWidget(self.port_spin, 1, 1)
        conn_layout.addWidget(QLabel("Channel:"), 2, 0)
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(["CH1 (30V/3A)", "CH2 (30V/3A)", "CH3 (5V/3A)"])
        conn_layout.addWidget(self.channel_combo, 2, 1)
        self.connect_button = QPushButton("Connect")
        conn_layout.addWidget(self.connect_button, 3, 0, 1, 2)
        self.identity_label = QLabel("Not connected")
        self.identity_label.setWordWrap(True)
        conn_layout.addWidget(self.identity_label, 4, 0, 1, 2)
        layout.addWidget(conn_group)

        # Charge settings
        settings_group = QGroupBox("Charge Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.addWidget(QLabel("Charge Voltage:"), 0, 0)
        self.voltage_spin = QDoubleSpinBox()
        self.voltage_spin.setRange(0.0, CHANNEL_LIMITS[1][0])
        self.voltage_spin.setDecimals(3)
        self.voltage_spin.setSingleStep(0.1)
        self.voltage_spin.setValue(4.2)
        self.voltage_spin.setSuffix(" V")
        settings_layout.addWidget(self.voltage_spin, 0, 1)
        settings_layout.addWidget(QLabel("Charge Current:"), 1, 0)
        self.current_spin = QDoubleSpinBox()
        self.current_spin.setRange(0.001, CHANNEL_LIMITS[1][1])
        self.current_spin.setDecimals(3)
        self.current_spin.setSingleStep(0.1)
        self.current_spin.setValue(1.0)
        self.current_spin.setSuffix(" A")
        settings_layout.addWidget(self.current_spin, 1, 1)
        settings_layout.addWidget(QLabel("Term. Current:"), 2, 0)
        self.term_current_spin = QDoubleSpinBox()
        self.term_current_spin.setRange(0.001, CHANNEL_LIMITS[1][1])
        self.term_current_spin.setDecimals(3)
        self.term_current_spin.setSingleStep(0.01)
        self.term_current_spin.setValue(0.05)
        self.term_current_spin.setSuffix(" A")
        self.term_current_spin.setToolTip(
            "Charge ends when CV-mode current stays below this for 5 seconds"
        )
        settings_layout.addWidget(self.term_current_spin, 2, 1)
        settings_layout.addWidget(QLabel("Safety Timeout:"), 3, 0)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 48)
        self.timeout_spin.setValue(8)
        self.timeout_spin.setSuffix(" h")
        settings_layout.addWidget(self.timeout_spin, 3, 1)
        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start Charge")
        self.start_button.setEnabled(False)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        settings_layout.addLayout(button_row, 4, 0, 1, 2)
        layout.addWidget(settings_group)

        # Live status
        status_group = QGroupBox("Charge Status")
        status_layout = QGridLayout(status_group)
        status_layout.addWidget(QLabel("Voltage:"), 0, 0)
        self.voltage_label = QLabel("--")
        status_layout.addWidget(self.voltage_label, 0, 1)
        status_layout.addWidget(QLabel("Current:"), 1, 0)
        self.current_label = QLabel("--")
        status_layout.addWidget(self.current_label, 1, 1)
        status_layout.addWidget(QLabel("Power:"), 2, 0)
        self.power_label = QLabel("--")
        status_layout.addWidget(self.power_label, 2, 1)
        status_layout.addWidget(QLabel("Mode:"), 3, 0)
        self.mode_label = QLabel("--")
        status_layout.addWidget(self.mode_label, 3, 1)
        status_layout.addWidget(QLabel("Elapsed:"), 4, 0)
        self.elapsed_label = QLabel("--")
        status_layout.addWidget(self.elapsed_label, 4, 1)
        self.state_label = QLabel("Idle")
        self.state_label.setAlignment(Qt.AlignCenter)
        status_layout.addWidget(self.state_label, 5, 0, 1, 2)
        layout.addWidget(status_group)

        layout.addStretch()

        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)

    # --- connection ---

    @Slot()
    def _on_connect_clicked(self) -> None:
        if self.charger.is_connected:
            self._stop_if_charging()
            self.charger.disconnect()
            self._set_connected_ui(False)
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "Charger", "Enter the DP832A IP address or hostname.")
            return
        try:
            self.charger.set_channel(self.channel_combo.currentIndex() + 1)
            self.charger.connect(host, self.port_spin.value())
        except ChargerError as e:
            QMessageBox.critical(self, "Charger", str(e))
            return
        self.identity_label.setText(self.charger.identity)
        self._set_connected_ui(True)

    @Slot(int)
    def _on_channel_changed(self, index: int) -> None:
        channel = index + 1
        max_v, max_a = CHANNEL_LIMITS[channel]
        self.voltage_spin.setMaximum(max_v)
        self.current_spin.setMaximum(max_a)
        self.term_current_spin.setMaximum(max_a)
        if self.charger.is_connected:
            self.charger.set_channel(channel)
        self._on_settings_changed()

    # --- charge lifecycle ---

    @Slot()
    def _on_start_clicked(self) -> None:
        if not self.charger.is_connected:
            QMessageBox.warning(self, "Charger", "Connect to the DP832A first.")
            return
        volts = self.voltage_spin.value()
        amps = self.current_spin.value()
        ok = (
            self.charger.set_voltage(volts)
            and self.charger.set_current(amps)
            and self.charger.set_ovp(volts + OVP_MARGIN_V)
            and self.charger.output_on()
        )
        if not ok:
            QMessageBox.warning(
                self, "Charger", "Failed to start charge (device busy or unreachable)."
            )
            return
        self._monitor = ChargeMonitor(
            termination_current_a=self.term_current_spin.value(),
            timeout_s=self.timeout_spin.value() * 3600.0,
        )
        self._monitor.start(time.monotonic())
        self._set_charging_ui(True)
        self.state_label.setText("Charging…")
        self._tick_timer.start()

    @Slot()
    def _on_stop_clicked(self) -> None:
        self._stop_if_charging()
        self.state_label.setText("Stopped by user")

    def _stop_if_charging(self) -> None:
        if self._monitor and self._monitor.state == ChargeState.CHARGING:
            self.charger.output_off()
        self._monitor = None
        self._tick_timer.stop()
        self._set_charging_ui(False)

    @Slot()
    def _on_tick(self) -> None:
        if not self._monitor or self._monitor.state != ChargeState.CHARGING:
            return
        now = time.monotonic()
        self.elapsed_label.setText(self._format_elapsed(self._monitor.elapsed_s(now)))
        status = self.charger.last_status
        if status is None:
            return
        state = self._monitor.update(status, now)
        if state == ChargeState.COMPLETE:
            self._finish_charge("Charge complete (current tapered below cutoff)")
        elif state == ChargeState.TIMED_OUT:
            self._finish_charge("Charge stopped: safety timeout reached")
        elif state == ChargeState.FAULT:
            self._finish_charge("Charge stopped: output turned off unexpectedly")

    def _finish_charge(self, message: str) -> None:
        self.charger.output_off()
        self._monitor = None
        self._tick_timer.stop()
        self._set_charging_ui(False)
        self.state_label.setText(message)

    # --- status display (GUI thread, via signals) ---

    @Slot(object)
    def _on_charger_status(self, status) -> None:
        self.voltage_label.setText(f"{status.voltage_v:.3f} V")
        self.current_label.setText(f"{status.current_a:.3f} A")
        self.power_label.setText(f"{status.power_w:.3f} W")
        self.mode_label.setText(status.mode if status.output_on else "OFF")

    @Slot(str)
    def _on_charger_error(self, message: str) -> None:
        self.state_label.setText(message)

    # --- UI state helpers ---

    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.host_edit.setEnabled(not connected)
        self.port_spin.setEnabled(not connected)
        self.start_button.setEnabled(connected)
        if not connected:
            self.identity_label.setText("Not connected")
            self.stop_button.setEnabled(False)
            for label in (self.voltage_label, self.current_label, self.power_label, self.mode_label):
                label.setText("--")
            self.state_label.setText("Idle")

    def _set_charging_ui(self, charging: bool) -> None:
        self.start_button.setEnabled(not charging and self.charger.is_connected)
        self.stop_button.setEnabled(charging)
        self.connect_button.setEnabled(not charging)
        self.channel_combo.setEnabled(not charging)
        for spin in (self.voltage_spin, self.current_spin, self.term_current_spin, self.timeout_spin):
            spin.setEnabled(not charging)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

    # --- session persistence (CLAUDE.md Test Automation panel pattern) ---

    def _connect_save_signals(self) -> None:
        self.host_edit.editingFinished.connect(self._on_settings_changed)
        self.port_spin.valueChanged.connect(self._on_settings_changed)
        self.voltage_spin.valueChanged.connect(self._on_settings_changed)
        self.current_spin.valueChanged.connect(self._on_settings_changed)
        self.term_current_spin.valueChanged.connect(self._on_settings_changed)
        self.timeout_spin.valueChanged.connect(self._on_settings_changed)
        # channel_combo saves via _on_channel_changed

    def _on_settings_changed(self) -> None:
        if not self._loading_settings:
            self._save_session()

    def _save_session(self) -> None:
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_file, "w") as f:
                json.dump(
                    {
                        "host": self.host_edit.text(),
                        "port": self.port_spin.value(),
                        "channel": self.channel_combo.currentIndex() + 1,
                        "voltage": self.voltage_spin.value(),
                        "current": self.current_spin.value(),
                        "termination_current": self.term_current_spin.value(),
                        "timeout_hours": self.timeout_spin.value(),
                    },
                    f,
                    indent=2,
                )
        except OSError:
            pass

    def _load_session(self) -> None:
        if not self._session_file.exists():
            return
        try:
            with open(self._session_file) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self._loading_settings = True
        try:
            self.host_edit.setText(data.get("host", ""))
            self.port_spin.setValue(data.get("port", RigolDP832A.DEFAULT_PORT))
            self.channel_combo.setCurrentIndex(data.get("channel", 1) - 1)
            self.voltage_spin.setValue(data.get("voltage", 4.2))
            self.current_spin.setValue(data.get("current", 1.0))
            self.term_current_spin.setValue(data.get("termination_current", 0.05))
            self.timeout_spin.setValue(data.get("timeout_hours", 8))
        finally:
            self._loading_settings = False

    # --- app shutdown ---

    def shutdown(self) -> None:
        """Stop any active charge and disconnect. Called from MainWindow.closeEvent."""
        self._stop_if_charging()
        self.charger.disconnect()
```

- [ ] **Step 2: Verify the module imports and existing tests still pass**

Run: `uv run --extra dev python -c "from load_test_bench.gui.dp832a_charger_panel import DP832AChargerPanel; print('ok')"`
Expected: prints `ok` (constructing the widget needs a QApplication — the smoke test happens in Task 5)

Run: `uv run --extra dev pytest`
Expected: all tests PASS (118 pre-existing + 32 new)

- [ ] **Step 3: Commit**

```bash
git add load_test_bench/gui/dp832a_charger_panel.py
git commit -m "Add Battery Charging panel for Rigol DP832A"
```

---

### Task 5: MainWindow Integration, Smoke Test, Docs

**Files:**
- Modify: `load_test_bench/gui/main_window.py` (import block ~line 55, tab creation ~line 477, `closeEvent` ~line 4199 — search for the anchors below, line numbers drift)
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes (Task 4): `DP832AChargerPanel()`, `DP832AChargerPanel.shutdown()`.
- Produces: nothing new — final wiring.

- [ ] **Step 1: Add the import**

In `load_test_bench/gui/main_window.py`, after the line `from .power_bank_panel import PowerBankPanel` add:

```python
from .dp832a_charger_panel import DP832AChargerPanel
```

- [ ] **Step 2: Add the tab**

In `_create_ui`, find this block:

```python
        self.battery_charger_panel = BatteryChargerPanel()
        charger_idx = self.bottom_tabs.addTab(self.battery_charger_panel, "Battery Charger Output")
        self.bottom_tabs.setTabToolTip(charger_idx, "Monitor and log battery charging sessions")
```

and insert directly after it:

```python
        self.dp832a_charger_panel = DP832AChargerPanel()
        dp832a_idx = self.bottom_tabs.addTab(self.dp832a_charger_panel, "Battery Charging")
        self.bottom_tabs.setTabToolTip(
            dp832a_idx, "Charge a battery using a Rigol DP832A power supply (LAN)"
        )
```

(The `_wip_tab_indices = set()` line and History tab come after this — do not disturb their order; the History tab must remain last.)

- [ ] **Step 3: Shut the charger down on close**

In `closeEvent`, find:

```python
        if self.device:
            self.device.disconnect()
```

and insert directly before it:

```python
        self.dp832a_charger_panel.shutdown()
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run --extra dev pytest`
Expected: all PASS

- [ ] **Step 5: Smoke-test the app**

Per CLAUDE.md launch rules: kill any previously-launched instance first (by its background task ID — never blanket `pkill python`), then launch non-blocking:

Run (in background): `uv run python -m load_test_bench.main`

Verify:
- App starts with no traceback in the output.
- A "Battery Charging" tab appears in the Test Automation tab row, before "History".
- With no DP832A on the network: entering a bogus IP and clicking Connect shows a "Cannot reach DP832A" error dialog after ~2 s and the app stays responsive.
- Adjusting a spinbox and restarting the app restores the value (session persistence, `sessions/dp832a_charger_session.json`).

Then kill the background instance.

- [ ] **Step 6: Update CLAUDE.md**

In `CLAUDE.md`, after the "### Test Automation (`load_test_bench/automation/`)" section, add:

```markdown
### Rigol DP832A Charger (LAN)

The "Battery Charging" tab charges a battery from a Rigol DP832A bench supply
over its LAN interface (raw SCPI on TCP port 5555, stdlib socket - no VISA).

- `protocol/dp832a_protocol.py` - pure SCPI build/parse (`DP832AProtocol`),
  `ChargerStatus` dataclass, `CHANNEL_LIMITS` (CH1/CH2: 32 V/3.2 A, CH3: 5.3 V/3.2 A)
- `protocol/rigol_dp832a.py` - `RigolDP832A` transport: 1 Hz poll thread,
  `GUI_LOCK_TIMEOUT` on commands, status/error callbacks (poll thread - GUI
  consumers must marshal through a Qt Signal)
- `automation/charge_monitor.py` - `ChargeMonitor`: CC-CV termination
  (complete when CV-mode current stays below the cutoff for 5 consecutive
  samples), safety timeout, fault on unexpected output-off
- `gui/dp832a_charger_panel.py` - self-contained panel that OWNS its charger
  device (unlike the DL24 panels, it does not use `MainWindow.device`);
  `MainWindow` only adds the tab and calls `panel.shutdown()` in `closeEvent`
- OVP is set automatically to charge voltage + 0.1 V at charge start
- Session file: `sessions/dp832a_charger_session.json` (host, port, channel,
  setpoints)
```

Also update the "Future Test Types" list in the "Graph Display Configuration by Test Type" section: change `- Battery Charger: TBD` to `- Battery Charging (DP832A): no graph in v1 (live readout only)`.

Also update the "Test Coverage" section counts: 150 tests across 9 files, adding:

```markdown
- `test_dp832a_protocol.py` (17) - DP832A SCPI build/parse
- `test_rigol_dp832a.py` (7) - DP832A LAN driver (fake socket)
- `test_charge_monitor.py` (8) - CC-CV charge termination state machine
```

(Adjust the counts to whatever `uv run --extra dev pytest` actually reports.)

- [ ] **Step 7: Commit**

```bash
git add load_test_bench/gui/main_window.py CLAUDE.md
git commit -m "Wire DP832A Battery Charging tab into main window"
```

---

## Future Work (explicitly out of scope)

- Charge-curve plotting and database logging of charge sessions
- Automated charge→rest→discharge cycle testing (charger + DL24 coordination)
- Battery chemistry presets for charge voltage/current
- Async (non-blocking) connect — `connect()` currently blocks the GUI thread for up to ~2 s
