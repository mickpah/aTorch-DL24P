"""Actuating safety layer: conservative invariants that cut hardware outputs.

Deliberately separate from alerts/ (notify-only, user-tunable). Rules are
pure; the supervisor latches the first trip and notifies via on_trip. All
actuation happens on the engine thread - observe_* run on device poll
threads and must stay microsecond-cheap.
"""

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class SafetyConfig:
    mosfet_temp_max_c: float = 80.0
    ext_temp_max_c: Optional[float] = 60.0  # None disables the rule
    psu_current_max_a: Optional[float] = None  # disabled until configured
    stale_status_timeout_s: float = 10.0
    temp_hysteresis_c: float = 5.0


@dataclass(frozen=True)
class Trip:
    rule: str
    message: str


class SafetyRules:
    def __init__(self, config: SafetyConfig) -> None:
        self.config = config

    def evaluate_load(self, status) -> list:
        trips = []
        if status.mosfet_temp_c >= self.config.mosfet_temp_max_c:
            trips.append(
                Trip(
                    "mosfet_over_temp",
                    f"load mosfet {status.mosfet_temp_c}°C >= {self.config.mosfet_temp_max_c}°C",
                )
            )
        if (
            self.config.ext_temp_max_c is not None
            and status.ext_temp_c > 0  # 0 = probe absent
            and status.ext_temp_c >= self.config.ext_temp_max_c
        ):
            trips.append(
                Trip(
                    "ext_over_temp",
                    f"external probe {status.ext_temp_c}°C >= {self.config.ext_temp_max_c}°C",
                )
            )
        return trips

    def evaluate_psu(self, status) -> list:
        trips = []
        if (
            self.config.psu_current_max_a is not None
            and status.current_a >= self.config.psu_current_max_a
        ):
            trips.append(
                Trip(
                    "psu_over_current",
                    f"psu current {status.current_a} A >= {self.config.psu_current_max_a} A",
                )
            )
        return trips

    def is_clear_load(self, status) -> bool:
        margin = self.config.temp_hysteresis_c
        if status.mosfet_temp_c >= self.config.mosfet_temp_max_c - margin:
            return False
        if (
            self.config.ext_temp_max_c is not None
            and status.ext_temp_c > 0
            and status.ext_temp_c >= self.config.ext_temp_max_c - margin
        ):
            return False
        return True

    def is_clear_psu(self, status) -> bool:
        if self.config.psu_current_max_a is None:
            return True
        return status.current_a < self.config.psu_current_max_a

    @property
    def stale_status_timeout_s(self) -> float:
        return self.config.stale_status_timeout_s


class SafetySupervisor:
    """Latching trip supervisor fed by both device status pipelines."""

    def __init__(self, rules: SafetyRules, on_trip: Optional[Callable[[str], None]] = None) -> None:
        self._rules = rules
        self._on_trip = on_trip
        self._lock = threading.Lock()
        self._tripped = False
        self._trip_reason = ""
        self._load_status = None
        self._load_seen_s: Optional[float] = None
        self._psu_status = None
        self._psu_seen_s: Optional[float] = None

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def observe_load(self, status, now_s: float) -> None:
        with self._lock:
            self._load_status = status
            self._load_seen_s = now_s
            trips = self._rules.evaluate_load(status)
        self._latch(trips)

    def observe_psu(self, status, now_s: float) -> None:
        with self._lock:
            self._psu_status = status
            self._psu_seen_s = now_s
            trips = self._rules.evaluate_psu(status)
        self._latch(trips)

    def check_stale(self, now_s: float) -> None:
        """Engine-thread watchdog: no fresh status while an output is on."""
        timeout = self._rules.stale_status_timeout_s
        trips = []
        with self._lock:
            if (
                self._load_status is not None
                and getattr(self._load_status, "load_on", False)
                and self._load_seen_s is not None
                and now_s - self._load_seen_s >= timeout
            ):
                trips.append(
                    Trip("load_status_stale", "load status stale while output on")
                )
            if (
                self._psu_status is not None
                and getattr(self._psu_status, "output_on", False)
                and self._psu_seen_s is not None
                and now_s - self._psu_seen_s >= timeout
            ):
                trips.append(Trip("psu_status_stale", "psu status stale while output on"))
        self._latch(trips)

    def forget_load(self) -> None:
        """Clear stored load status/timestamp so check_stale has nothing to
        trip on - used for deliberate disconnects, not faults."""
        with self._lock:
            self._load_status = None
            self._load_seen_s = None

    def forget_psu(self) -> None:
        """Clear stored PSU status/timestamp so check_stale has nothing to
        trip on - used for deliberate disconnects, not faults."""
        with self._lock:
            self._psu_status = None
            self._psu_seen_s = None

    def try_reset(self) -> bool:
        """Clear the latch - only when every rule currently evaluates clear."""
        with self._lock:
            if not self._tripped:
                return True
            load_clear = self._load_status is None or self._rules.is_clear_load(
                self._load_status
            )
            psu_clear = self._psu_status is None or self._rules.is_clear_psu(
                self._psu_status
            )
            if load_clear and psu_clear:
                self._tripped = False
                self._trip_reason = ""
                return True
            return False

    def _latch(self, trips) -> None:
        if not trips:
            return
        fire = False
        reason = None
        with self._lock:
            if not self._tripped:
                self._tripped = True
                self._trip_reason = "; ".join(trip.message for trip in trips)
                reason = self._trip_reason
                fire = True
        if fire and self._on_trip is not None:
            try:
                self._on_trip(reason)
            except Exception:
                pass
