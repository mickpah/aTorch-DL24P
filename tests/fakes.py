"""Hand-rolled fake devices for job engine tests (house style: no mock lib).

Contract used across test files: every command is recorded in .calls, .status
is returned by last_status, .connected by is_connected, and while
fail_commands > 0 every command returns False and decrements it.
"""

from typing import Optional

from load_test_bench.jobs.devices import MeterStatus


class _FakeBase:
    def __init__(self) -> None:
        self.calls: list = []
        self.status = None
        self.connected = True
        self.fail_commands = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def last_status(self):
        return self.status

    def _command(self, name: str, *args) -> bool:
        self.calls.append((name, *args))
        if self.fail_commands > 0:
            self.fail_commands -= 1
            return False
        return True


class FakeLoad(_FakeBase):
    def __init__(self) -> None:
        super().__init__()
        self.on = False

    def set_mode(self, mode: int, value: Optional[float] = None) -> bool:
        return self._command("set_mode", mode, value)

    def set_current(self, current_a: float) -> bool:
        return self._command("set_current", current_a)

    def set_resistance(self, resistance_ohm: float) -> bool:
        return self._command("set_resistance", resistance_ohm)

    def set_voltage_cutoff(self, voltage: float) -> bool:
        return self._command("set_voltage_cutoff", voltage)

    def reset_counters(self) -> bool:
        return self._command("reset_counters")

    def turn_on(self) -> bool:
        ok = self._command("turn_on")
        if ok:
            self.on = True
        return ok

    def turn_off(self) -> bool:
        ok = self._command("turn_off")
        if ok:
            self.on = False
        return ok


class FakePsu(_FakeBase):
    def __init__(self) -> None:
        super().__init__()
        self.output_on_state = False

    def set_voltage(self, volts: float) -> bool:
        return self._command("set_voltage", volts)

    def set_current(self, amps: float) -> bool:
        return self._command("set_current", amps)

    def set_ovp(self, volts: float) -> bool:
        return self._command("set_ovp", volts)

    def output_on(self) -> bool:
        ok = self._command("output_on")
        if ok:
            self.output_on_state = True
        return ok

    def output_off(self) -> bool:
        ok = self._command("output_off")
        if ok:
            self.output_on_state = False
        return ok


class FakeMeter(_FakeBase):
    def __init__(self, voltage_v: float = 0.0) -> None:
        super().__init__()
        self.status = MeterStatus(voltage_v=voltage_v)
