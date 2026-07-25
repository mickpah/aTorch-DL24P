"""Job execution: thread-free JobExecutor plus the one-thread JobEngine.

JobExecutor owns every orchestration decision and is driven by step(now_s) -
tests call step() directly with a fake clock, no threads. JobEngine is the
thin daemon-thread wrapper: step, then wait up to TICK_INTERVAL_S on a wake
event so safety trips and operator commands get sub-second reaction.

All device commands issued during jobs happen inside step() - i.e. on the
engine thread in production. Snapshot callbacks also fire there; the GUI
bridge marshals them onto the Qt thread.
"""

import sys
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from ..data.database import Database
from ..data.models import Reading, TestSession
from .devices import DeviceRegistry
from .ledger import JobLedger
from .model import JobSnapshot, JobSpec, JobState, PhaseResult, PhaseState
from .phases import Phase, PhaseContext, build_phase
from .recovery import make_safe


class JobExecutor:
    HEARTBEAT_INTERVAL_S = 5.0
    READING_INTERVAL_S = 1.0
    ENTER_RETRY_LIMIT = 3

    def __init__(
        self,
        ledger: JobLedger,
        registry: DeviceRegistry,
        database: Database,
        reading_sink: Optional[Callable[[int, Reading], None]] = None,
        supervisor=None,
        settle: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._database = database
        self._reading_sink = reading_sink or (lambda session_id, reading: None)
        self._supervisor = supervisor
        self._settle = settle if settle is not None else time.sleep
        self._snapshot_callbacks: List[Callable[[JobSnapshot], None]] = []
        self.last_progress: dict = {}

        self._job_id: Optional[int] = None
        self._spec: Optional[JobSpec] = None
        self._state = JobState.PENDING
        self._phase: Optional[Phase] = None
        self._phase_entered = False
        self._phase_index = 0
        self._phase_session: Optional[TestSession] = None
        self._enter_attempts = 0
        self._job_started_s = 0.0
        self._last_heartbeat_s = 0.0
        self._last_reading_s = -1.0
        self._pause_requested = False
        self._stop_requested = False
        self._abort_reason: Optional[str] = None
        self._safety_handled = False

    @property
    def ledger(self) -> JobLedger:
        return self._ledger

    @property
    def has_active_job(self) -> bool:
        return self._job_id is not None

    @property
    def active_job_id(self) -> Optional[int]:
        return self._job_id

    def add_snapshot_callback(self, callback: Callable[[JobSnapshot], None]) -> None:
        self._snapshot_callbacks.append(callback)

    # --- operator commands (thread-safe: plain flag writes) ---

    def submit(self, spec: JobSpec) -> int:
        if self._supervisor is not None and self._supervisor.tripped:
            raise RuntimeError(
                f"Safety lockout active: {self._supervisor.trip_reason}"
            )
        return self._ledger.create_job(spec)

    def pause(self) -> None:
        self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def abort_for_safety(self, reason: str) -> None:
        self._abort_reason = f"safety: {reason}"

    # --- the tick ---

    def step(self, now_s: float) -> None:
        if self._supervisor is not None:
            self._supervisor.check_stale(now_s)
            if self._supervisor.tripped:
                if self._job_id is not None and self._abort_reason is None:
                    self._abort_reason = f"safety: {self._supervisor.trip_reason}"
                elif self._job_id is None and not self._safety_handled:
                    self._make_safe()
                    self._safety_handled = True
            else:
                self._safety_handled = False

        if self._job_id is None:
            if self._supervisor is not None and self._supervisor.tripped:
                # Latch also blocks queued-job pickup, not just submission -
                # a PENDING job must not silently start while tripped.
                return
            self._maybe_start_next(now_s)
            return
        if self._abort_reason is not None:
            reason = self._abort_reason
            self._abort_reason = None
            self._finish_job(JobState.FAULTED, reason, now_s)
            return
        if self._stop_requested:
            self._stop_requested = False
            self._finish_job(JobState.STOPPED, "stopped by operator", now_s)
            return
        if self._state == JobState.RUNNING and self._pause_requested:
            if self._phase is not None:
                self._phase.on_pause(self._ctx(), now_s)
            self._state = JobState.PAUSED
            self._ledger.set_job_state(self._job_id, JobState.PAUSED)
            self._emit(now_s, "paused")
            return
        if self._state == JobState.PAUSED:
            if not self._pause_requested:
                if self._phase is not None and self._phase.on_resume(self._ctx(), now_s):
                    self._state = JobState.RUNNING
                    self._ledger.set_job_state(self._job_id, JobState.RUNNING)
                    self._emit(now_s, "resumed")
                else:
                    self._finish_job(JobState.FAULTED, "resume failed", now_s)
            else:
                self._heartbeat_if_due(now_s)
            return
        # RUNNING
        if not self._phase_entered:
            self._enter_current_phase(now_s)
            return
        self._heartbeat_if_due(now_s)
        self._capture_reading(now_s)
        tick = self._phase.tick(self._ctx(), now_s)
        if tick.done:
            self._complete_phase(tick.result, now_s)
        else:
            self._emit(now_s, "")

    # --- reporter interface (phases call this) ---

    def on_progress(self, progress: dict) -> None:
        self.last_progress = progress

    # --- internals ---

    def _ctx(self) -> PhaseContext:
        return PhaseContext(
            load=self._registry.load,
            psu=self._registry.psu,
            meter=self._registry.meter,
            report=self,
            settle=self._settle,
        )

    def _maybe_start_next(self, now_s: float) -> None:
        pending = self._ledger.next_pending_job()
        if pending is None:
            return
        job_id, spec = pending
        self._job_id, self._spec = job_id, spec
        self._state = JobState.RUNNING
        self._phase = None
        self._phase_entered = False
        self._phase_index = 0
        self._phase_session = None
        self._enter_attempts = 0
        self._job_started_s = now_s
        self._last_heartbeat_s = now_s
        self._last_reading_s = -1.0
        # A job that ended while paused/stop-requested must not leak those
        # control flags into the next job (it would silently self-pause or
        # self-stop on the very next tick).
        self._pause_requested = False
        self._stop_requested = False
        self._ledger.mark_job_running(job_id)
        self._enter_current_phase(now_s)

    def _enter_current_phase(self, now_s: float) -> None:
        spec = self._spec.phases[self._phase_index]
        if self._phase is None:
            try:
                self._phase = build_phase(spec)
            except ValueError as e:
                self._finish_job(JobState.FAULTED, f"invalid phase spec: {e}", now_s)
                return
        if not self._phase.on_enter(self._ctx(), now_s):
            self._enter_attempts += 1
            if self._enter_attempts >= self.ENTER_RETRY_LIMIT:
                self._finish_job(
                    JobState.FAULTED,
                    f"phase '{spec.phase_type}' failed to start", now_s,
                )
            return  # retried next step (on_enter is idempotent)
        self._enter_attempts = 0
        self._phase_entered = True
        session_id = None
        if self._phase.creates_session:
            session = TestSession(
                name=f"{self._spec.name} - {spec.phase_type} {self._phase_index + 1}",
                start_time=datetime.now(),
                battery_name=self._spec.battery_name,
                notes=self._spec.notes,
                test_type=spec.phase_type,
                settings=dict(spec.params),
            )
            session.id = self._database.create_session(session)
            self._database.set_session_status(session.id, "running")
            phase_row = self._ledger.phase_row_id(self._job_id, self._phase_index)
            if phase_row is not None:
                self._database.link_session_to_phase(session.id, phase_row)
            self._phase_session = session
            session_id = session.id
        self._ledger.set_current_phase(self._job_id, self._phase_index)
        self._ledger.set_phase_state(
            self._job_id, self._phase_index, PhaseState.RUNNING, session_id=session_id
        )
        self._emit(now_s, f"{spec.phase_type} started")

    def _complete_phase(self, result: PhaseResult, now_s: float) -> None:
        if result.state == PhaseState.FAULTED:
            self._finish_job(JobState.FAULTED, result.reason, now_s)
            return
        self._phase.on_exit(self._ctx(), result.reason)
        self._finalize_phase_session("completed")
        self._ledger.set_phase_state(
            self._job_id, self._phase_index, result.state, result=result
        )
        if self._phase_index + 1 < len(self._spec.phases):
            self._phase_index += 1
            self._phase = None
            self._phase_entered = False
            self._enter_attempts = 0
            self._emit(now_s, "phase complete")
        else:
            self._finish_job(JobState.COMPLETED, result.reason, now_s)

    def _finish_job(self, state: JobState, reason: str, now_s: float) -> None:
        if state != JobState.COMPLETED:
            self._make_safe()
            if self._phase is not None:
                try:
                    self._phase.on_exit(self._ctx(), reason)
                except Exception:
                    pass
                phase_state = (
                    PhaseState.FAULTED
                    if state == JobState.FAULTED
                    else PhaseState.INTERRUPTED
                )
                self._ledger.set_phase_state(
                    self._job_id,
                    self._phase_index,
                    phase_state,
                    result=PhaseResult(phase_state, reason=reason),
                )
        session_status = {
            JobState.COMPLETED: "completed",
            JobState.FAULTED: "faulted",
        }.get(state, "interrupted")
        self._finalize_phase_session(session_status)
        fault = reason if state in (JobState.FAULTED, JobState.INTERRUPTED) else None
        self._ledger.set_job_state(self._job_id, state, fault_reason=fault)
        self._state = state
        self._emit(now_s, reason)
        self._job_id = None
        self._spec = None
        self._phase = None
        self._phase_entered = False
        self._phase_session = None
        self._state = JobState.PENDING

    def _finalize_phase_session(self, status: str) -> None:
        session = self._phase_session
        if session is None:
            return
        session.end_time = datetime.now()
        self._database.update_session(session)
        self._database.set_session_status(session.id, status)
        self._database.commit()
        self._phase_session = None

    def _make_safe(self) -> None:
        make_safe(
            load=self._registry.load, psu=self._registry.psu, sleep=self._settle
        )

    def _heartbeat_if_due(self, now_s: float) -> None:
        if now_s - self._last_heartbeat_s >= self.HEARTBEAT_INTERVAL_S:
            self._ledger.heartbeat(self._job_id)
            self._last_heartbeat_s = now_s

    def _capture_reading(self, now_s: float) -> None:
        if self._phase_session is None or self._phase_session.id is None:
            return
        if now_s - self._last_reading_s < self.READING_INTERVAL_S:
            return
        load = self._registry.load
        status = load.last_status if load is not None else None
        if status is None:
            return
        # aux_voltage_v stays NULL until the meter driver lands (see spec)
        reading = Reading(
            timestamp=datetime.now(),
            voltage_v=status.voltage_v,
            current_a=status.current_a,
            power_w=getattr(status, "power_w", 0.0),
            energy_wh=getattr(status, "energy_wh", 0.0),
            capacity_mah=getattr(status, "capacity_mah", 0.0),
            mosfet_temp_c=getattr(status, "mosfet_temp_c", 0),
            ext_temp_c=getattr(status, "ext_temp_c", 0),
            fan_speed_rpm=getattr(status, "fan_speed_rpm", 0),
            load_r_ohm=getattr(status, "load_r_ohm", None),
            battery_r_ohm=getattr(status, "battery_r_ohm", None),
            runtime_s=getattr(status, "runtime_seconds", 0),
        )
        self._reading_sink(self._phase_session.id, reading)
        self._last_reading_s = now_s

    def _emit(self, now_s: float, message: str) -> None:
        if self._job_id is None or self._spec is None:
            return
        snapshot = JobSnapshot(
            job_id=self._job_id,
            state=self._state,
            spec=self._spec,
            phase_index=self._phase_index,
            phase_state=PhaseState.RUNNING if self._phase else PhaseState.PENDING,
            elapsed_s=now_s - self._job_started_s,
            message=message,
        )
        for callback in self._snapshot_callbacks:
            try:
                callback(snapshot)
            except Exception:
                pass


class JobEngine:
    """One daemon thread driving the executor with an interruptible wait."""

    TICK_INTERVAL_S = 1.0

    def __init__(self, executor: JobExecutor) -> None:
        self._executor = executor
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def executor(self) -> JobExecutor:
        return self._executor

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._running = False
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        # Deterministic outputs-off at exit: the engine thread is joined (or
        # never started), so the executor is guaranteed thread-free here -
        # run one final synchronous step to consume any pending stop/abort
        # and make hardware safe before the process exits.
        self._executor.step(time.monotonic())

    def wake(self) -> None:
        self._wake.set()

    def submit(self, spec: JobSpec) -> int:
        job_id = self._executor.submit(spec)
        self.wake()
        return job_id

    def pause(self) -> None:
        self._executor.pause()
        self.wake()

    def resume(self) -> None:
        self._executor.resume()
        self.wake()

    def stop(self) -> None:
        self._executor.stop()
        self.wake()

    def _run(self) -> None:
        while self._running:
            try:
                self._executor.step(time.monotonic())
            except Exception as e:
                # The engine thread must never die; executor faults are
                # handled internally - this catches only genuine bugs, but
                # leave a diagnostic trail instead of swallowing silently.
                print(f"JobEngine step failed: {e!r}", file=sys.stderr)
            self._wake.wait(self.TICK_INTERVAL_S)
            self._wake.clear()
