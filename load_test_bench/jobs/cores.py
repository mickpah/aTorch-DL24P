"""Pure decision cores for job phases.

House pattern (ChargeMonitor): no I/O, no Qt, no clocks - callers supply
now_s (time.monotonic()) and status snapshots; cores return decisions.
voltage_override is the meter-role seam: when a phase is configured with
voltage_source="meter", the shell passes the independent meter voltage and
it takes precedence over the load's own readout.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DischargeOutcome(Enum):
    CONTINUE = "continue"
    VOLTAGE_CUTOFF = "voltage_cutoff"
    DEVICE_STOPPED = "device_stopped"
    TIMEOUT = "timeout"


class DischargeCore:
    """Discharge termination: voltage cutoff, device-side stop, max duration.

    DEVICE_STOPPED only counts after START_GRACE_S - right after turn_on the
    first poll can still report the load off.
    """

    START_GRACE_S = 3.0

    def __init__(self, voltage_cutoff: float, max_duration_s: Optional[float] = None) -> None:
        self.voltage_cutoff = voltage_cutoff
        self.max_duration_s = max_duration_s
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> DischargeOutcome:
        if self.max_duration_s is not None and now_s - self._started_at >= self.max_duration_s:
            return DischargeOutcome.TIMEOUT
        if status is None:
            return DischargeOutcome.CONTINUE
        voltage = voltage_override if voltage_override is not None else status.voltage_v
        if voltage <= self.voltage_cutoff:
            return DischargeOutcome.VOLTAGE_CUTOFF
        if not status.load_on and now_s - self._started_at >= self.START_GRACE_S:
            return DischargeOutcome.DEVICE_STOPPED
        return DischargeOutcome.CONTINUE


class RestOutcome(Enum):
    CONTINUE = "continue"
    DONE = "done"


class RestCore:
    """Trivial duration wait with everything off."""

    def __init__(self, duration_s: float) -> None:
        self.duration_s = duration_s
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, now_s: float) -> RestOutcome:
        if now_s - self._started_at >= self.duration_s:
            return RestOutcome.DONE
        return RestOutcome.CONTINUE


class TimedOutcome(Enum):
    CONTINUE = "continue"
    DONE = "done"
    VOLTAGE_CUTOFF = "voltage_cutoff"


class TimedCore:
    """Fixed-duration run with an optional safety voltage cutoff."""

    def __init__(self, duration_s: float, voltage_cutoff: Optional[float] = None) -> None:
        self.duration_s = duration_s
        self.voltage_cutoff = voltage_cutoff
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> TimedOutcome:
        if self.voltage_cutoff is not None and status is not None:
            voltage = voltage_override if voltage_override is not None else status.voltage_v
            if voltage <= self.voltage_cutoff:
                return TimedOutcome.VOLTAGE_CUTOFF
        if now_s - self._started_at >= self.duration_s:
            return TimedOutcome.DONE
        return TimedOutcome.CONTINUE


class SteppedAction(Enum):
    CONTINUE = "continue"
    SET_VALUE = "set_value"
    REST_OFF = "rest_off"
    VOLTAGE_CUTOFF = "voltage_cutoff"
    DONE = "done"


@dataclass
class SteppedUpdate:
    action: SteppedAction
    value: Optional[float] = None
    step_index: int = 0


class SteppedCore:
    """Stepped sweep over an explicit (value, dwell_s) list.

    The core returns commands (SET_VALUE / REST_OFF / DONE); the shell
    executes them - which is what makes the sweep logic unit-testable.
    Uniform sweeps come from build_sweep_steps (divisions+1 semantics,
    matching the battery-load/charger panels).
    """

    def __init__(self, steps, voltage_cutoff: Optional[float] = None,
                 rest_between_steps_s: float = 0.0) -> None:
        if not steps:
            raise ValueError("steps must not be empty")
        self.steps = [(float(value), float(dwell)) for value, dwell in steps]
        self.voltage_cutoff = voltage_cutoff
        self.rest_between_steps_s = rest_between_steps_s
        self._index = 0
        self._in_rest = False
        self._mark = 0.0  # start time of the current dwell or rest

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def current_step(self) -> int:
        return self._index

    def start(self, now_s: float) -> float:
        self._index = 0
        self._in_rest = False
        self._mark = now_s
        return self.steps[0][0]

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> SteppedUpdate:
        if self.voltage_cutoff is not None and status is not None:
            voltage = voltage_override if voltage_override is not None else status.voltage_v
            if voltage <= self.voltage_cutoff:
                return SteppedUpdate(SteppedAction.VOLTAGE_CUTOFF, step_index=self._index)
        if self._in_rest:
            if now_s - self._mark >= self.rest_between_steps_s:
                self._in_rest = False
                self._mark = now_s
                return SteppedUpdate(
                    SteppedAction.SET_VALUE,
                    value=self.steps[self._index][0],
                    step_index=self._index,
                )
            return SteppedUpdate(SteppedAction.CONTINUE, step_index=self._index)
        _, dwell = self.steps[self._index]
        if now_s - self._mark >= dwell:
            self._index += 1
            if self._index >= len(self.steps):
                return SteppedUpdate(SteppedAction.DONE, step_index=len(self.steps))
            if self.rest_between_steps_s > 0:
                self._in_rest = True
                self._mark = now_s
                return SteppedUpdate(SteppedAction.REST_OFF, step_index=self._index)
            self._mark = now_s
            return SteppedUpdate(
                SteppedAction.SET_VALUE,
                value=self.steps[self._index][0],
                step_index=self._index,
            )
        return SteppedUpdate(SteppedAction.CONTINUE, step_index=self._index)


def build_sweep_steps(start_value: float, end_value: float, divisions: int, dwell_s: float):
    """divisions+1 evenly spaced steps from start to end (panel semantics)."""
    if divisions < 1:
        return [(float(start_value), float(dwell_s))]
    step_size = (end_value - start_value) / divisions
    return [
        (start_value + index * step_size, float(dwell_s))
        for index in range(divisions + 1)
    ]
