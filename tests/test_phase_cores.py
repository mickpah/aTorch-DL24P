"""Tests for the pure phase decision cores (injected clock, no I/O)."""

from dataclasses import dataclass

from load_test_bench.jobs.cores import (
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


@dataclass
class Status:
    """Minimal DeviceStatus stand-in: cores only read voltage_v and load_on."""

    voltage_v: float = 4.0
    load_on: bool = True


class TestDischargeCore:
    def test_continues_above_cutoff(self):
        core = DischargeCore(voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(Status(voltage_v=3.7), now_s=10.0) == DischargeOutcome.CONTINUE

    def test_voltage_cutoff(self):
        core = DischargeCore(voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(Status(voltage_v=3.0), now_s=10.0) == DischargeOutcome.VOLTAGE_CUTOFF

    def test_meter_override_drives_cutoff(self):
        """With a meter, its voltage decides - not the load's own readout."""
        core = DischargeCore(voltage_cutoff=3.0)
        core.start(now_s=0.0)
        outcome = core.update(Status(voltage_v=3.4), now_s=10.0, voltage_override=2.95)
        assert outcome == DischargeOutcome.VOLTAGE_CUTOFF

    def test_device_stop_detected_after_grace(self):
        """Load-off right after start is ignored (turn_on settling); later it ends the phase."""
        core = DischargeCore(voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(Status(load_on=False), now_s=1.0) == DischargeOutcome.CONTINUE
        assert core.update(Status(load_on=False), now_s=4.0) == DischargeOutcome.DEVICE_STOPPED

    def test_timeout(self):
        core = DischargeCore(voltage_cutoff=3.0, max_duration_s=100.0)
        core.start(now_s=0.0)
        assert core.update(Status(), now_s=99.0) == DischargeOutcome.CONTINUE
        assert core.update(Status(), now_s=100.0) == DischargeOutcome.TIMEOUT

    def test_missing_status_continues(self):
        """No fresh status is not a decision - staleness is the engine's job."""
        core = DischargeCore(voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(None, now_s=10.0) == DischargeOutcome.CONTINUE


class TestRestCore:
    def test_rest_completes_after_duration(self):
        core = RestCore(duration_s=60.0)
        core.start(now_s=100.0)
        assert core.update(now_s=159.0) == RestOutcome.CONTINUE
        assert core.update(now_s=160.0) == RestOutcome.DONE


class TestTimedCore:
    def test_runs_to_duration(self):
        core = TimedCore(duration_s=30.0)
        core.start(now_s=0.0)
        assert core.update(Status(), now_s=29.0) == TimedOutcome.CONTINUE
        assert core.update(Status(), now_s=30.0) == TimedOutcome.DONE

    def test_optional_safety_cutoff(self):
        core = TimedCore(duration_s=30.0, voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(Status(voltage_v=2.9), now_s=5.0) == TimedOutcome.VOLTAGE_CUTOFF


class TestSteppedCore:
    def test_start_returns_first_value(self):
        core = SteppedCore(steps=[(0.5, 10.0), (1.0, 10.0)])
        assert core.start(now_s=0.0) == 0.5
        assert core.total_steps == 2

    def test_advances_after_dwell(self):
        core = SteppedCore(steps=[(0.5, 10.0), (1.0, 10.0)])
        core.start(now_s=0.0)
        update = core.update(Status(), now_s=5.0)
        assert update.action == SteppedAction.CONTINUE
        update = core.update(Status(), now_s=10.0)
        assert update.action == SteppedAction.SET_VALUE
        assert update.value == 1.0
        assert update.step_index == 1

    def test_done_after_last_step(self):
        core = SteppedCore(steps=[(0.5, 10.0)])
        core.start(now_s=0.0)
        assert core.update(Status(), now_s=10.0).action == SteppedAction.DONE

    def test_rest_between_steps(self):
        """With rest configured: dwell -> REST_OFF -> rest -> SET_VALUE."""
        core = SteppedCore(steps=[(0.5, 10.0), (1.0, 10.0)], rest_between_steps_s=5.0)
        core.start(now_s=0.0)
        assert core.update(Status(), now_s=10.0).action == SteppedAction.REST_OFF
        assert core.update(Status(), now_s=12.0).action == SteppedAction.CONTINUE
        update = core.update(Status(), now_s=15.0)
        assert update.action == SteppedAction.SET_VALUE
        assert update.value == 1.0

    def test_voltage_cutoff_ends_sweep(self):
        core = SteppedCore(steps=[(0.5, 10.0), (1.0, 10.0)], voltage_cutoff=3.0)
        core.start(now_s=0.0)
        assert core.update(Status(voltage_v=2.9), now_s=1.0).action == SteppedAction.VOLTAGE_CUTOFF

    def test_empty_steps_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            SteppedCore(steps=[])


class TestBuildSweepSteps:
    def test_divisions_plus_one_semantics(self):
        """0.1 A to 0.5 A in 4 divisions = 5 steps (panel semantics preserved)."""
        steps = build_sweep_steps(0.1, 0.5, divisions=4, dwell_s=10.0)
        values = [round(v, 3) for v, _ in steps]
        assert values == [0.1, 0.2, 0.3, 0.4, 0.5]
        assert all(d == 10.0 for _, d in steps)

    def test_zero_divisions_is_single_step(self):
        assert build_sweep_steps(1.0, 2.0, divisions=0, dwell_s=5.0) == [(1.0, 5.0)]
