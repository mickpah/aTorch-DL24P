"""Tests for the thread-free JobExecutor (fake devices, fake clock, tmp DB)."""

from dataclasses import dataclass
from typing import Optional

import pytest

from load_test_bench.data.database import Database
from load_test_bench.jobs.devices import DeviceRegistry
from load_test_bench.jobs.engine import JobEngine, JobExecutor
from load_test_bench.jobs.ledger import JobLedger
from load_test_bench.jobs.model import JobSpec, PhaseSpec
from load_test_bench.jobs.safety import SafetyConfig, SafetyRules, SafetySupervisor
from tests.fakes import FakeLoad, FakePsu


@dataclass
class LoadStatus:
    """DeviceStatus stand-in with every field the executor reads."""

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


class Harness:
    def __init__(self, tmp_path, supervisor=None):
        self.db = Database(tmp_path / "tests.db")
        self.ledger = JobLedger(self.db)
        self.registry = DeviceRegistry()
        self.load = FakeLoad()
        self.load.status = LoadStatus()
        self.registry.register("load", self.load)
        self.readings = []
        self.snapshots = []
        self.executor = JobExecutor(
            ledger=self.ledger,
            registry=self.registry,
            database=self.db,
            reading_sink=lambda session_id, reading: self.readings.append(
                (session_id, reading)
            ),
            supervisor=supervisor,
            settle=lambda seconds: None,
        )
        self.executor.add_snapshot_callback(self.snapshots.append)

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


def discharge_spec(**params):
    merged = {"current_a": 1.0, "voltage_cutoff": 3.0}
    merged.update(params)
    return JobSpec(
        name="discharge test", job_type="discharge",
        phases=(PhaseSpec("discharge", merged),),
    )


class TestHappyPath:
    def test_discharge_job_runs_to_voltage_cutoff(self, harness):
        job_id = harness.executor.submit(discharge_spec())
        harness.run(0.0, 5.0)
        assert harness.load.on is True
        assert harness.ledger.get_job(job_id)["state"] == "RUNNING"
        harness.load.status = LoadStatus(voltage_v=2.9)
        harness.run(6.0, 7.0)
        job = harness.ledger.get_job(job_id)
        assert job["state"] == "COMPLETED"
        assert harness.load.on is False
        phase = harness.ledger.get_phases(job_id)[0]
        assert phase["state"] == "COMPLETED"
        assert "voltage_cutoff" in phase["result_json"]
        session_row = harness.db._conn.execute(
            "SELECT status, end_time, job_phase_id FROM sessions"
        ).fetchone()
        assert session_row[0] == "completed"
        assert session_row[1] is not None
        assert session_row[2] == phase["id"]

    def test_readings_flow_to_sink_about_once_per_second(self, harness):
        harness.executor.submit(discharge_spec())
        harness.run(0.0, 10.0)
        assert 9 <= len(harness.readings) <= 12
        session_id, reading = harness.readings[0]
        assert reading.voltage_v == 4.0
        assert isinstance(session_id, int)

    def test_cycle_job_advances_through_phases(self, harness):
        spec = JobSpec(
            name="cycle", job_type="cycle",
            phases=(
                PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0}),
                PhaseSpec("rest", {"duration_s": 5}),
                PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0}),
            ),
        )
        job_id = harness.executor.submit(spec)
        harness.run(0.0, 4.0)
        harness.load.status = LoadStatus(voltage_v=2.9)  # ends discharge 1
        harness.run(5.0, 6.0)
        assert harness.ledger.get_job(job_id)["current_phase_index"] >= 1
        harness.load.status = LoadStatus(voltage_v=4.0)  # rest, then discharge 2
        harness.run(7.0, 14.0)
        harness.load.status = LoadStatus(voltage_v=2.9)
        harness.run(15.0, 17.0)
        job = harness.ledger.get_job(job_id)
        assert job["state"] == "COMPLETED"
        states = [p["state"] for p in harness.ledger.get_phases(job_id)]
        assert states == ["COMPLETED", "COMPLETED", "COMPLETED"]
        # one session per data phase, none for rest
        count = harness.db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 2

    def test_heartbeat_advances(self, harness):
        job_id = harness.executor.submit(discharge_spec())
        harness.run(0.0, 1.0)
        first = harness.ledger.get_job(job_id)["heartbeat_at"]
        harness.run(2.0, 7.0)
        assert harness.ledger.get_job(job_id)["heartbeat_at"] != first


class TestControl:
    def test_pause_and_resume(self, harness):
        job_id = harness.executor.submit(discharge_spec())
        harness.run(0.0, 2.0)
        harness.executor.pause()
        harness.run(3.0, 4.0)
        assert harness.load.on is False
        assert harness.ledger.get_job(job_id)["state"] == "PAUSED"
        harness.executor.resume()
        harness.run(5.0, 6.0)
        assert harness.load.on is True
        assert harness.ledger.get_job(job_id)["state"] == "RUNNING"

    def test_stop_finalizes_as_stopped(self, harness):
        job_id = harness.executor.submit(discharge_spec())
        harness.run(0.0, 2.0)
        harness.executor.stop()
        harness.run(3.0, 4.0)
        assert harness.ledger.get_job(job_id)["state"] == "STOPPED"
        assert harness.load.on is False
        assert harness.executor.has_active_job is False
        session_status = harness.db._conn.execute(
            "SELECT status FROM sessions"
        ).fetchone()[0]
        assert session_status == "interrupted"

    def test_enter_failure_faults_after_retries(self, harness):
        harness.load.fail_commands = 999
        job_id = harness.executor.submit(discharge_spec())
        harness.run(0.0, 5.0)
        job = harness.ledger.get_job(job_id)
        assert job["state"] == "FAULTED"
        assert "failed to start" in job["fault_reason"]

    def test_pause_then_stop_does_not_leak_into_next_job(self, harness):
        """C1 regression: a job that ends while paused must not leave
        _pause_requested set - the next job would silently self-pause and
        turn the load back off on its very next tick."""
        job1 = harness.executor.submit(discharge_spec())
        harness.run(0.0, 1.0)
        harness.executor.pause()
        harness.run(2.0, 2.0)
        assert harness.ledger.get_job(job1)["state"] == "PAUSED"
        harness.executor.stop()
        harness.run(3.0, 3.0)
        assert harness.ledger.get_job(job1)["state"] == "STOPPED"

        job2 = harness.executor.submit(discharge_spec())
        harness.run(4.0, 8.0)
        job2_row = harness.ledger.get_job(job2)
        assert job2_row["state"] == "RUNNING"
        assert harness.load.on is True


class TestSafetyIntegration:
    def make_supervised(self, tmp_path):
        supervisor = SafetySupervisor(
            SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0))
        )
        return Harness(tmp_path, supervisor=supervisor), supervisor

    def test_trip_mid_job_faults_and_makes_safe(self, tmp_path):
        harness, supervisor = self.make_supervised(tmp_path)
        try:
            psu = FakePsu()
            psu.output_on_state = True
            harness.registry.register("psu", psu)
            job_id = harness.executor.submit(discharge_spec())
            harness.run(0.0, 2.0)
            supervisor.observe_load(LoadStatus(mosfet_temp_c=95), now_s=3.0)
            harness.run(3.0, 4.0)
            job = harness.ledger.get_job(job_id)
            assert job["state"] == "FAULTED"
            assert "safety" in job["fault_reason"]
            assert harness.load.on is False
            assert psu.output_on_state is False
        finally:
            harness.close()

    def test_submit_refused_while_tripped(self, tmp_path):
        harness, supervisor = self.make_supervised(tmp_path)
        try:
            supervisor.observe_load(LoadStatus(mosfet_temp_c=95), now_s=0.0)
            with pytest.raises(RuntimeError):
                harness.executor.submit(discharge_spec())
        finally:
            harness.close()

    def test_idle_trip_still_makes_safe_once(self, tmp_path):
        harness, supervisor = self.make_supervised(tmp_path)
        try:
            harness.load.on = True
            supervisor.observe_load(LoadStatus(mosfet_temp_c=95), now_s=0.0)
            harness.run(1.0, 3.0)
            assert harness.load.on is False
            off_count = harness.load.calls.count(("turn_off",))
            harness.run(4.0, 6.0)
            assert harness.load.calls.count(("turn_off",)) == off_count
        finally:
            harness.close()

    def test_tripped_before_first_step_blocks_queued_pickup(self, tmp_path):
        """C2 regression: the latch must block a PENDING job from starting
        on step(), not just refuse new submissions."""
        harness, supervisor = self.make_supervised(tmp_path)
        try:
            job_id = harness.executor.submit(discharge_spec())
            supervisor.observe_load(LoadStatus(mosfet_temp_c=95), now_s=0.5)
            assert supervisor.tripped is True
            harness.run(1.0, 4.0)
            job = harness.ledger.get_job(job_id)
            assert job["state"] == "PENDING"
            assert harness.load.on is False
            assert harness.load.calls.count(("turn_on",)) == 0
        finally:
            harness.close()


class TestEngineShutdown:
    def test_shutdown_processes_pending_stop_deterministically(self, harness):
        """I3 regression: JobEngine.shutdown() must consume a pending stop
        and make hardware safe even if the engine thread was never started
        and no step() was called after stop()."""
        job_id = harness.executor.submit(discharge_spec())
        harness.executor.step(0.0)  # picks up the job -> RUNNING, load on
        assert harness.ledger.get_job(job_id)["state"] == "RUNNING"
        assert harness.load.on is True

        harness.executor.stop()  # request stop, but don't step
        engine = JobEngine(harness.executor)  # thread never started
        engine.shutdown()

        assert harness.ledger.get_job(job_id)["state"] == "STOPPED"
        assert harness.load.on is False


class TestMeterCapture:
    def test_reading_carries_meter_voltage(self, tmp_path):
        """When a meter is registered, its voltage is logged as aux_voltage_v."""
        from load_test_bench.jobs.devices import MeterStatus
        from tests.fakes import FakeMeter

        harness = Harness(tmp_path)
        try:
            meter = FakeMeter()
            meter.status = MeterStatus(voltage_v=3.815)
            harness.registry.register("meter", meter)
            harness.executor.submit(discharge_spec())
            harness.run(0.0, 3.0)
            assert harness.readings, "expected at least one reading"
            _, reading = harness.readings[0]
            assert reading.aux_voltage_v == 3.815
        finally:
            harness.close()

    def test_reading_aux_none_without_meter(self, tmp_path):
        harness = Harness(tmp_path)
        try:
            harness.executor.submit(discharge_spec())
            harness.run(0.0, 3.0)
            _, reading = harness.readings[0]
            assert reading.aux_voltage_v is None
        finally:
            harness.close()
