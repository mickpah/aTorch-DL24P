"""Charge termination state machine for PSU-based CC-CV battery charging.

Pure logic - no I/O, no Qt. The GUI panel feeds it one ChargerStatus per poll
tick plus a monotonic timestamp, and acts on the returned state. Timestamps
are caller-supplied so tests are deterministic.
"""

from enum import Enum

from ..protocol.dp832a_protocol import ChargerStatus

DEFAULT_TAPER_SAMPLES = 5


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
        taper_samples: int = DEFAULT_TAPER_SAMPLES,
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
