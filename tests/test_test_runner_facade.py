"""Tests for the TestRunner facade over the job engine."""

from dataclasses import dataclass
from typing import Optional

import pytest

from load_test_bench.automation.profiles import (
    CycleProfile,
    DischargeProfile,
    SteppedProfile,
    TimedProfile,
)
from load_test_bench.automation.test_runner import (
    TestRunner,
    TestState,
    profile_to_spec,
)
from load_test_bench.data.database import Database
from load_test_bench.jobs.devices import DeviceRegistry
from load_test_bench.jobs.engine import JobEngine, JobExecutor
from load_test_bench.jobs.ledger import JobLedger
from tests.fakes import FakeLoad


@dataclass
class LoadStatus:
    voltage_v: float = 4.0
    current_a: float = 1.0
    power_w: float = 4.0
    energy_wh: float = 0.5
    capacity_mah: float = 500.0
    mosfet_temp_c: int = 40
    ext_temp_c: int = 25
    fan_speed_rpm: int = 0
    load_r_ohm: Optional[float] = None
    battery_r_ohm: Optional[float] = None
    runtime_seconds: int = 0
    load_on: bool = True


class TestProfileConversion:
    def test_discharge_profile(self):
        spec = profile_to_spec(
            DischargeProfile(name="d", current_a=1.5, voltage_cutoff=3.2), "batt", ""
        )
        assert spec.job_type == "discharge"
        assert len(spec.phases) == 1
        assert spec.phases[0].params["current_a"] == 1.5
        assert spec.battery_name == "batt"

    def test_cycle_profile_expands_with_rests_between(self):
        spec = profile_to_spec(
            CycleProfile(name="c", num_cycles=3, rest_between_cycles_s=60), "", ""
        )
        types = [p.phase_type for p in spec.phases]
        assert types == ["discharge", "rest", "discharge", "rest", "discharge"]

    def test_timed_profile(self):
        spec = profile_to_spec(TimedProfile(name="t", duration_s=120), "", "")
        assert spec.phases[0].phase_type == "timed"
        assert spec.phases[0].params["duration_s"] == 120

    def test_stepped_profile(self):
        profile = SteppedProfile(
            name="s",
            steps=[{"current_a": 0.5, "duration_s": 10}, {"current_a": 1.0, "duration_s": 10}],
        )
        spec = profile_to_spec(profile, "", "")
        assert spec.phases[0].phase_type == "stepped"
        assert spec.phases[0].params["steps"] == [[0.5, 10], [1.0, 10]]

    def test_unknown_profile_rejected(self):
        with pytest.raises(ValueError):
            profile_to_spec(object(), "", "")


class Harness:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "tests.db")
        self.registry = DeviceRegistry()
        self.load = FakeLoad()
        self.load.status = LoadStatus()
        self.registry.register("load", self.load)
        self.executor = JobExecutor(
            ledger=JobLedger(self.db),
            registry=self.registry,
            database=self.db,
            settle=lambda seconds: None,
        )
        self.engine = JobEngine(self.executor)  # thread never started in tests
        self.runner = TestRunner(self.load, self.db, self.engine)

    def run(self, start_s, end_s, step_s=1.0):
        now = start_s
        while now <= end_s:
            self.executor.step(now)
            now += step_s

    def close(self):
        self.db.close()


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


class TestFacadeLifecycle:
    def test_start_refused_without_connected_device(self, harness):
        harness.load.connected = False
        assert harness.runner.start(DischargeProfile(name="d")) is False
        harness.runner.device = None
        assert harness.runner.start(DischargeProfile(name="d")) is False

    def test_full_discharge_run(self, harness):
        progresses, completions = [], []
        harness.runner.set_progress_callback(progresses.append)
        harness.runner.set_complete_callback(completions.append)
        assert harness.runner.start(
            DischargeProfile(name="d", current_a=1.0, voltage_cutoff=3.0),
            battery_name="18650",
        ) is True
        assert harness.runner.is_running is True
        harness.run(0.0, 3.0)
        assert harness.runner.state == TestState.RUNNING
        harness.load.status = LoadStatus(voltage_v=2.9)
        harness.run(4.0, 5.0)
        assert harness.runner.state == TestState.VOLTAGE_CUTOFF
        assert harness.runner.is_running is False
        assert len(completions) == 1
        session = completions[0]
        assert session.battery_name == "18650"
        assert session.end_time is not None
        assert any(p.state == TestState.RUNNING for p in progresses)

    def test_start_refused_while_running(self, harness):
        harness.runner.start(DischargeProfile(name="d"))
        harness.run(0.0, 1.0)
        assert harness.runner.start(DischargeProfile(name="d2")) is False

    def test_stop_maps_to_completed(self, harness):
        harness.runner.start(DischargeProfile(name="d"))
        harness.run(0.0, 1.0)
        harness.runner.stop()
        harness.run(2.0, 3.0)
        assert harness.runner.state == TestState.COMPLETED
        assert harness.load.on is False

    def test_cycle_progress_reports_cycles(self, harness):
        harness.runner.start(CycleProfile(name="c", num_cycles=2, rest_between_cycles_s=2))
        harness.run(0.0, 2.0)
        assert harness.runner.progress.total_cycles == 2
        assert harness.runner.progress.current_cycle == 1

    def test_start_refused_while_stop_is_winding_down(self, harness):
        """A non-blocking stop() must not allow a new start() to orphan the
        old job's terminal snapshot."""
        harness.runner.start(DischargeProfile(name="d"))
        harness.run(0.0, 1.0)
        harness.runner.stop()
        # Engine has not ticked yet - old job still in flight
        assert harness.runner.start(DischargeProfile(name="d2")) is False
        harness.run(2.0, 3.0)  # terminal snapshot arrives, _job_id cleared
        assert harness.runner.start(DischargeProfile(name="d3")) is True

    def test_device_stopped_maps_to_voltage_cutoff(self, harness):
        """Device-side auto-stop keeps its legacy VOLTAGE_CUTOFF semantics."""
        harness.runner.start(DischargeProfile(name="d", voltage_cutoff=3.0))
        harness.run(0.0, 2.0)
        harness.load.status = LoadStatus(load_on=False)  # device stopped itself
        harness.run(4.0, 6.0)  # past the 3 s device-stop grace
        assert harness.runner.state == TestState.VOLTAGE_CUTOFF
