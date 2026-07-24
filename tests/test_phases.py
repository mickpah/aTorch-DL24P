"""Tests for the phase actuation shells (fake devices, injected clock)."""

from dataclasses import dataclass

import pytest

from load_test_bench.jobs.model import PhaseSpec, PhaseState
from load_test_bench.jobs.phases import (
    CONTINUE,
    PhaseContext,
    build_phase,
)
from tests.fakes import FakeLoad, FakePsu


@dataclass
class LoadStatus:
    """DeviceStatus stand-in with the fields phases read."""

    voltage_v: float = 4.0
    load_on: bool = True
    capacity_mah: float = 100.0
    energy_wh: float = 0.4


def ctx_with(load=None, psu=None):
    return PhaseContext(load=load, psu=psu, settle=lambda seconds: None)


class TestDischargePhase:
    def make(self):
        return build_phase(
            PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0})
        )

    def test_on_enter_establishes_device_state(self):
        phase, load = self.make(), FakeLoad()
        assert phase.on_enter(ctx_with(load), now_s=0.0) is True
        names = [call[0] for call in load.calls]
        assert names == ["reset_counters", "set_current", "set_voltage_cutoff", "turn_on"]
        assert ("set_current", 1.0) in load.calls
        assert load.on is True

    def test_on_enter_fails_without_connected_load(self):
        phase = self.make()
        load = FakeLoad()
        load.connected = False
        assert phase.on_enter(ctx_with(load), now_s=0.0) is False
        assert phase.on_enter(ctx_with(None), now_s=0.0) is False

    def test_tick_runs_then_completes_on_cutoff(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus(voltage_v=3.8)
        assert phase.tick(ctx_with(load), now_s=5.0) is CONTINUE
        load.status = LoadStatus(voltage_v=2.99)
        tick = phase.tick(ctx_with(load), now_s=6.0)
        assert tick.done is True
        assert tick.result.state == PhaseState.COMPLETED
        assert tick.result.reason == "voltage_cutoff"
        assert tick.result.metrics["capacity_mah"] == 100.0

    def test_pause_and_resume(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        phase.on_pause(ctx_with(load), now_s=5.0)
        assert load.on is False
        assert phase.on_resume(ctx_with(load), now_s=10.0) is True
        assert load.on is True

    def test_on_exit_turns_load_off(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        phase.on_exit(ctx_with(load), reason="voltage_cutoff")
        assert load.on is False

    def test_pause_time_does_not_count_toward_max_duration(self):
        """Wall-clock time spent paused must not consume the run budget."""
        phase = build_phase(
            PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0,
                                    "max_duration_s": 100.0})
        )
        load = FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        phase.on_pause(ctx_with(load), now_s=50.0)
        assert phase.on_resume(ctx_with(load), now_s=250.0) is True  # 200 s paused
        assert phase.tick(ctx_with(load), now_s=290.0) is CONTINUE   # 90 s run time
        tick = phase.tick(ctx_with(load), now_s=300.0)               # 100 s run time
        assert tick.done is True
        assert tick.result.reason == "timeout"


class TestRestPhase:
    def test_rest_turns_everything_off_and_completes(self):
        phase = build_phase(PhaseSpec("rest", {"duration_s": 60}))
        assert phase.creates_session is False
        load, psu = FakeLoad(), FakePsu()
        load.on = True
        psu.output_on_state = True
        assert phase.on_enter(ctx_with(load, psu), now_s=0.0) is True
        assert load.on is False
        assert psu.output_on_state is False
        assert phase.tick(ctx_with(load, psu), now_s=59.0) is CONTINUE
        tick = phase.tick(ctx_with(load, psu), now_s=60.0)
        assert tick.done is True
        assert tick.result.reason == "rest_complete"


class TestTimedPhase:
    def test_runs_for_duration(self):
        phase = build_phase(PhaseSpec("timed", {"current_a": 0.5, "duration_s": 30}))
        load = FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        assert phase.tick(ctx_with(load), now_s=29.0) is CONTINUE
        tick = phase.tick(ctx_with(load), now_s=30.0)
        assert tick.done is True
        assert tick.result.reason == "duration_complete"


class TestSteppedPhase:
    def make(self, **extra):
        params = {"steps": [[0.5, 10.0], [1.0, 10.0]], "voltage_cutoff": 3.0}
        params.update(extra)
        return build_phase(PhaseSpec("stepped", params))

    def test_on_enter_applies_first_step(self):
        phase, load = self.make(), FakeLoad()
        assert phase.on_enter(ctx_with(load), now_s=0.0) is True
        assert ("set_current", 0.5) in load.calls
        assert load.on is True

    def test_advances_to_next_value_after_dwell(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        phase.tick(ctx_with(load), now_s=5.0)
        phase.tick(ctx_with(load), now_s=10.0)
        assert ("set_current", 1.0) in load.calls

    def test_rest_between_steps_toggles_load(self):
        phase, load = self.make(rest_between_steps_s=5.0), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        phase.tick(ctx_with(load), now_s=10.0)  # dwell over -> REST_OFF
        assert load.on is False
        phase.tick(ctx_with(load), now_s=15.0)  # rest over -> SET_VALUE + turn_on
        assert load.on is True
        assert ("set_current", 1.0) in load.calls

    def test_sweep_completes(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        phase.tick(ctx_with(load), now_s=10.0)
        tick = phase.tick(ctx_with(load), now_s=20.0)
        assert tick.done is True
        assert tick.result.reason == "sweep_complete"

    def test_repeated_command_failures_fault_the_phase(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        load.fail_commands = 99
        tick1 = phase.tick(ctx_with(load), now_s=10.0)   # SET_VALUE fails (1)
        assert tick1.done is False
        tick2 = phase.tick(ctx_with(load), now_s=20.0)   # DONE would fire, but sweep
        # only 2 steps: craft a 4-step sweep instead for three failures
        phase = build_phase(
            PhaseSpec("stepped", {"steps": [[0.1, 1.0], [0.2, 1.0], [0.3, 1.0], [0.4, 1.0]]})
        )
        load = FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        load.fail_commands = 99
        assert phase.tick(ctx_with(load), now_s=1.0).done is False
        assert phase.tick(ctx_with(load), now_s=2.0).done is False
        tick = phase.tick(ctx_with(load), now_s=3.0)
        assert tick.done is True
        assert tick.result.state == PhaseState.FAULTED
        assert tick.result.reason == "device_command_failed"

    def test_sweep_params_build_steps(self):
        phase = build_phase(
            PhaseSpec("stepped", {"start_value": 0.1, "end_value": 0.5,
                                  "divisions": 4, "dwell_s": 10.0})
        )
        assert phase._core.total_steps == 5

    def test_pause_does_not_expire_the_current_dwell(self):
        """A long pause must not instantly advance the sweep on resume."""
        phase = self.make()
        load = FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        load.status = LoadStatus()
        phase.on_pause(ctx_with(load), now_s=6.0)
        assert phase.on_resume(ctx_with(load), now_s=106.0) is True
        update = phase.tick(ctx_with(load), now_s=108.0)  # 8 s of dwell elapsed
        assert update is CONTINUE
        update = phase.tick(ctx_with(load), now_s=110.0)  # 10 s of dwell elapsed
        assert ("set_current", 1.0) in load.calls


class TestBuildPhase:
    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError):
            build_phase(PhaseSpec("espresso", {}))

    def test_missing_params_rejected(self):
        with pytest.raises(ValueError):
            build_phase(PhaseSpec("discharge", {}))
