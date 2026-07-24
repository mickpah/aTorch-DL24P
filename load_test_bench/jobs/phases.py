"""Phase actuation shells: thin device I/O around the pure cores.

Prefect seam contract (see jobs/__init__.py): a phase takes JSON-serializable
PhaseSpec.params plus an injected PhaseContext, reports only through
PhaseReporter, returns a JSON-serializable PhaseResult, keeps tick()
non-blocking, and has idempotent edges - on_enter establishes full device
state from params; on_exit makes its device safe.

Domain neutrality: the engine dispatches purely through PHASE_TYPES. A new
domain (PSU burn-in, converter characterization, ...) adds a Phase subclass
and registers it here - engine, ledger, and recovery are untouched.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .cores import (
    DischargeCore,
    DischargeOutcome,
    RestCore,
    RestOutcome,
    SteppedAction,
    SteppedCore,
    TimedCore,
    TimedOutcome,
    build_sweep_steps,
)
from .model import PhaseResult, PhaseSpec, PhaseState


class PhaseReporter(Protocol):
    def on_progress(self, progress: dict) -> None: ...


@dataclass
class PhaseTick:
    done: bool = False
    result: Optional[PhaseResult] = None


CONTINUE = PhaseTick()  # shared sentinel - never mutate


def _no_settle(seconds: float) -> None:
    return None


@dataclass
class PhaseContext:
    """Everything a phase may touch - injected, nothing global, nothing Qt."""

    load: Optional[object] = None
    psu: Optional[object] = None
    meter: Optional[object] = None
    report: Optional[PhaseReporter] = None
    settle: Callable[[float], None] = field(default=_no_settle)


class Phase(ABC):
    creates_session = True  # phases that produce readings get a sessions row
    uses_load = True

    def __init__(self, spec: PhaseSpec) -> None:
        self.spec = spec
        self._paused_at: Optional[float] = None

    @abstractmethod
    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        """Establish full device state from params. Idempotent; True on success."""

    @abstractmethod
    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        """One non-blocking decision step."""

    def on_pause(self, ctx: PhaseContext, now_s: float) -> None:
        self._paused_at = now_s
        if ctx.load is not None:
            ctx.load.turn_off()

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        return True

    def _shift_after_pause(self, now_s: float) -> None:
        if self._paused_at is not None:
            self._core.shift(now_s - self._paused_at)
            self._paused_at = None

    def on_exit(self, ctx: PhaseContext, reason: str) -> None:
        if ctx.load is not None:
            ctx.load.turn_off()

    def _meter_voltage(self, ctx: PhaseContext) -> Optional[float]:
        """The meter-role seam: independent voltage when configured."""
        if self.spec.params.get("voltage_source") != "meter" or ctx.meter is None:
            return None
        status = ctx.meter.last_status
        return status.voltage_v if status is not None else None

    @staticmethod
    def _metrics(status) -> dict:
        if status is None:
            return {}
        return {
            "capacity_mah": getattr(status, "capacity_mah", 0.0),
            "energy_wh": getattr(status, "energy_wh", 0.0),
        }


class DischargePhase(Phase):
    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        self._current_a = float(params["current_a"])
        self._voltage_cutoff = float(params["voltage_cutoff"])
        max_duration = params.get("max_duration_s")
        self._core = DischargeCore(
            self._voltage_cutoff,
            float(max_duration) if max_duration is not None else None,
        )

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        ctx.settle(0.5)
        ok = load.set_current(self._current_a) and ok
        ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        ok = load.turn_on() and ok
        if ok:
            self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        outcome = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if outcome == DischargeOutcome.CONTINUE:
            return CONTINUE
        return PhaseTick(
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED, reason=outcome.value, metrics=self._metrics(status)
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        self._shift_after_pause(now_s)
        load = ctx.load
        if load is None:
            return False
        ok = load.set_current(self._current_a)
        return load.turn_on() and ok


class RestPhase(Phase):
    creates_session = False
    uses_load = False

    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        self._core = RestCore(float(spec.params["duration_s"]))

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        ok = True
        if ctx.load is not None and ctx.load.is_connected:
            ok = ctx.load.turn_off()
        if ctx.psu is not None and ctx.psu.is_connected:
            ok = ctx.psu.output_off() and ok
        self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        if self._core.update(now_s) == RestOutcome.DONE:
            return PhaseTick(
                done=True, result=PhaseResult(PhaseState.COMPLETED, reason="rest_complete")
            )
        return CONTINUE

    def on_pause(self, ctx: PhaseContext, now_s: float) -> None:
        return None  # nothing running


class TimedPhase(Phase):
    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        self._current_a = float(params["current_a"])
        cutoff = params.get("voltage_cutoff")
        self._voltage_cutoff = float(cutoff) if cutoff is not None else None
        self._core = TimedCore(float(params["duration_s"]), self._voltage_cutoff)

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        ctx.settle(0.5)
        ok = load.set_current(self._current_a) and ok
        if self._voltage_cutoff is not None:
            ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        ok = load.turn_on() and ok
        if ok:
            self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        outcome = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if outcome == TimedOutcome.CONTINUE:
            return CONTINUE
        reason = "duration_complete" if outcome == TimedOutcome.DONE else outcome.value
        return PhaseTick(
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED, reason=reason, metrics=self._metrics(status)
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        self._shift_after_pause(now_s)
        load = ctx.load
        if load is None:
            return False
        ok = load.set_current(self._current_a)
        return load.turn_on() and ok


class SteppedPhase(Phase):
    COMMAND_FAILURE_LIMIT = 3

    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        if "steps" in params:
            steps = [
                (step[0], step[1])
                if isinstance(step, (list, tuple))
                else (step["value"], step["dwell_s"])
                for step in params["steps"]
            ]
        else:
            steps = build_sweep_steps(
                params["start_value"],
                params["end_value"],
                int(params.get("divisions", 1)),
                params["dwell_s"],
            )
        self._mode = params.get("mode", "current")
        if self._mode not in ("current", "resistance"):
            raise ValueError(f"Unknown stepped mode: {self._mode}")
        cutoff = params.get("voltage_cutoff")
        self._voltage_cutoff = float(cutoff) if cutoff is not None else None
        self._core = SteppedCore(
            steps,
            voltage_cutoff=self._voltage_cutoff,
            rest_between_steps_s=float(params.get("rest_between_steps_s", 0.0)),
        )
        self._command_failures = 0
        self._after_rest = False

    def _apply_value(self, ctx: PhaseContext, value: float) -> bool:
        if self._mode == "resistance":
            return ctx.load.set_resistance(value)
        return ctx.load.set_current(value)

    def _note_command(self, ok: bool) -> None:
        self._command_failures = 0 if ok else self._command_failures + 1

    def _fault(self) -> PhaseTick:
        return PhaseTick(
            done=True,
            result=PhaseResult(PhaseState.FAULTED, reason="device_command_failed"),
        )

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        if self._voltage_cutoff is not None:
            ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        first_value = self._core.start(now_s)
        ok = self._apply_value(ctx, first_value) and ok
        ok = load.turn_on() and ok
        self._command_failures = 0
        self._after_rest = False
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        update = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if update.action == SteppedAction.CONTINUE:
            return CONTINUE
        if update.action == SteppedAction.SET_VALUE:
            ok = self._apply_value(ctx, update.value)
            if self._after_rest:
                ok = ctx.load.turn_on() and ok
                self._after_rest = False
            self._note_command(ok)
            if self._command_failures >= self.COMMAND_FAILURE_LIMIT:
                return self._fault()
            if ctx.report is not None:
                ctx.report.on_progress(
                    {"step": update.step_index, "total_steps": self._core.total_steps}
                )
            return CONTINUE
        if update.action == SteppedAction.REST_OFF:
            self._after_rest = True
            self._note_command(ctx.load.turn_off())
            if self._command_failures >= self.COMMAND_FAILURE_LIMIT:
                return self._fault()
            return CONTINUE
        if update.action == SteppedAction.VOLTAGE_CUTOFF:
            return PhaseTick(
                done=True,
                result=PhaseResult(
                    PhaseState.COMPLETED,
                    reason="voltage_cutoff",
                    metrics=self._metrics(status),
                ),
            )
        return PhaseTick(  # DONE
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED,
                reason="sweep_complete",
                metrics=self._metrics(status),
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        self._shift_after_pause(now_s)
        index = min(self._core.current_step, self._core.total_steps - 1)
        ok = self._apply_value(ctx, self._core.steps[index][0])
        return ctx.load.turn_on() and ok


PHASE_TYPES = {
    "discharge": DischargePhase,
    "rest": RestPhase,
    "timed": TimedPhase,
    "stepped": SteppedPhase,
}


def build_phase(spec: PhaseSpec) -> Phase:
    cls = PHASE_TYPES.get(spec.phase_type)
    if cls is None:
        raise ValueError(f"Unknown phase type: {spec.phase_type}")
    try:
        return cls(spec)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid params for phase '{spec.phase_type}': {e}") from e
