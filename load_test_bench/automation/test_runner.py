"""TestRunner - compatibility facade over the job engine (Stage-1 shim).

Preserves the public surface main_window and the panels use (start/stop/
pause/resume, progress/complete callbacks, .device) while execution lives in
jobs/engine.py. Callbacks fire on the engine thread - the same threading
semantics as the old worker-thread TestRunner. Deleted in Stage 5 once all
consumers build JobSpecs directly.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional

from ..data.database import Database
from ..data.models import TestSession
from ..jobs.engine import JobEngine
from ..jobs.model import (
    TERMINAL_JOB_STATES,
    JobSnapshot,
    JobSpec,
    JobState,
    PhaseSpec,
)
from .profiles import (
    CycleProfile,
    DischargeProfile,
    SteppedProfile,
    TestProfile,
    TimedProfile,
)


class TestState(Enum):
    """Test execution states."""

    IDLE = auto()
    STARTING = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()
    COMPLETED = auto()
    ERROR = auto()
    VOLTAGE_CUTOFF = auto()
    TIMEOUT = auto()


@dataclass
class TestProgress:
    """Current test progress information."""

    state: TestState
    elapsed_seconds: int = 0
    current_step: int = 0
    total_steps: int = 1
    current_cycle: int = 0
    total_cycles: int = 1
    message: str = ""


def profile_to_spec(profile: TestProfile, battery_name: str, notes: str) -> JobSpec:
    """Translate a legacy TestProfile into a declarative JobSpec."""
    if isinstance(profile, DischargeProfile):
        params = {
            "current_a": profile.current_a,
            "voltage_cutoff": profile.voltage_cutoff,
        }
        if profile.max_duration_s is not None:
            params["max_duration_s"] = profile.max_duration_s
        phases, job_type = (PhaseSpec("discharge", params),), "discharge"
    elif isinstance(profile, CycleProfile):
        phase_list = []
        for cycle in range(profile.num_cycles):
            phase_list.append(
                PhaseSpec(
                    "discharge",
                    {
                        "current_a": profile.current_a,
                        "voltage_cutoff": profile.voltage_cutoff,
                    },
                )
            )
            if cycle < profile.num_cycles - 1:
                phase_list.append(
                    PhaseSpec("rest", {"duration_s": profile.rest_between_cycles_s})
                )
        phases, job_type = tuple(phase_list), "cycle"
    elif isinstance(profile, TimedProfile):
        params = {"current_a": profile.current_a, "duration_s": profile.duration_s}
        if profile.voltage_cutoff is not None:
            params["voltage_cutoff"] = profile.voltage_cutoff
        phases, job_type = (PhaseSpec("timed", params),), "timed"
    elif isinstance(profile, SteppedProfile):
        steps = [[step["current_a"], step["duration_s"]] for step in profile.steps]
        params = {
            "steps": steps,
            "rest_between_steps_s": profile.rest_between_steps_s,
        }
        if profile.voltage_cutoff is not None:
            params["voltage_cutoff"] = profile.voltage_cutoff
        phases, job_type = (PhaseSpec("stepped", params),), "stepped"
    else:
        raise ValueError(f"Unknown profile type: {type(profile).__name__}")
    return JobSpec(
        name=profile.name,
        job_type=job_type,
        phases=phases,
        battery_name=battery_name,
        notes=notes,
    )


class TestRunner:
    def __init__(self, device, database: Database, engine: JobEngine) -> None:
        self.device = device  # panels read .device.is_connected
        self.database = database
        self._engine = engine
        self._job_id: Optional[int] = None
        self._is_cycle_job = False
        self._total_cycles = 1
        self._state = TestState.IDLE
        self._progress = TestProgress(state=TestState.IDLE)
        self._progress_callback: Optional[Callable[[TestProgress], None]] = None
        self._complete_callback: Optional[Callable[[TestSession], None]] = None
        engine.executor.add_snapshot_callback(self._on_snapshot)

    @property
    def state(self) -> TestState:
        return self._state

    @property
    def progress(self) -> TestProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._state in (TestState.STARTING, TestState.RUNNING, TestState.PAUSED)

    def set_progress_callback(self, callback: Callable[[TestProgress], None]) -> None:
        self._progress_callback = callback

    def set_complete_callback(self, callback: Callable[[TestSession], None]) -> None:
        self._complete_callback = callback

    def start(self, profile: TestProfile, battery_name: str = "", notes: str = "") -> bool:
        if self.is_running:
            return False
        if self.device is None or not self.device.is_connected:
            return False
        try:
            spec = profile_to_spec(profile, battery_name, notes)
        except ValueError:
            return False
        try:
            self._job_id = self._engine.submit(spec)
        except RuntimeError:  # safety lockout
            return False
        self._is_cycle_job = isinstance(profile, CycleProfile)
        self._total_cycles = profile.num_cycles if self._is_cycle_job else 1
        self._state = TestState.STARTING
        self._progress = TestProgress(
            state=TestState.STARTING,
            total_steps=len(spec.phases),
            total_cycles=self._total_cycles,
        )
        return True

    def stop(self) -> None:
        if self._job_id is not None:
            self._state = TestState.STOPPING
            self._engine.stop()

    def pause(self) -> None:
        if self._state == TestState.RUNNING:
            self._engine.pause()

    def resume(self) -> None:
        if self._state == TestState.PAUSED:
            self._engine.resume()

    # --- snapshot handling (engine thread) ---

    def _on_snapshot(self, snapshot: JobSnapshot) -> None:
        if snapshot.job_id != self._job_id:
            return
        state = self._map_state(snapshot)
        current_cycle = (
            min(snapshot.phase_index // 2 + 1, self._total_cycles)
            if self._is_cycle_job
            else 1
        )
        self._progress = TestProgress(
            state=state,
            elapsed_seconds=int(snapshot.elapsed_s),
            current_step=snapshot.phase_index + 1,
            total_steps=max(len(snapshot.spec.phases), 1),
            current_cycle=current_cycle,
            total_cycles=self._total_cycles,
            message=snapshot.message,
        )
        self._state = state
        if self._progress_callback:
            try:
                self._progress_callback(self._progress)
            except Exception:
                pass
        if snapshot.state in TERMINAL_JOB_STATES:
            self._finish(snapshot)

    def _map_state(self, snapshot: JobSnapshot) -> TestState:
        if snapshot.state == JobState.RUNNING:
            return TestState.RUNNING
        if snapshot.state == JobState.PAUSED:
            return TestState.PAUSED
        if snapshot.state == JobState.PENDING:
            return TestState.STARTING
        if snapshot.state == JobState.COMPLETED:
            if "voltage_cutoff" in snapshot.message:
                return TestState.VOLTAGE_CUTOFF
            if "timeout" in snapshot.message:
                return TestState.TIMEOUT
            return TestState.COMPLETED
        if snapshot.state == JobState.STOPPED:
            return TestState.COMPLETED  # old stop() also normalized to COMPLETED
        return TestState.ERROR  # FAULTED / INTERRUPTED

    def _finish(self, snapshot: JobSnapshot) -> None:
        job_id, self._job_id = self._job_id, None
        if self._complete_callback is None or job_id is None:
            return
        session = self._last_session(job_id)
        if session is not None:
            try:
                self._complete_callback(session)
            except Exception:
                pass

    def _last_session(self, job_id: int) -> Optional[TestSession]:
        for phase in reversed(self._engine.executor.ledger.get_phases(job_id)):
            if phase.get("session_id"):
                return self.database.get_session(
                    phase["session_id"], include_readings=True
                )
        return None
