"""Tests for the SQLite job ledger."""

import pytest

from load_test_bench.data.database import Database
from load_test_bench.jobs.ledger import JobLedger
from load_test_bench.jobs.model import (
    JobSpec,
    JobState,
    PhaseResult,
    PhaseSpec,
    PhaseState,
)


@pytest.fixture
def ledger(tmp_path):
    db = Database(tmp_path / "tests.db")
    yield JobLedger(db)
    db.close()


def make_spec():
    return JobSpec(
        name="cycle",
        job_type="cycle_test",
        phases=(
            PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0}),
            PhaseSpec("rest", {"duration_s": 60}),
        ),
    )


class TestJobLifecycle:
    def test_create_job_writes_job_and_phase_rows(self, ledger):
        job_id = ledger.create_job(make_spec())
        job = ledger.get_job(job_id)
        assert job["state"] == "PENDING"
        assert job["job_type"] == "cycle_test"
        phases = ledger.get_phases(job_id)
        assert [(p["phase_index"], p["phase_type"], p["state"]) for p in phases] == [
            (0, "discharge", "PENDING"),
            (1, "rest", "PENDING"),
        ]

    def test_spec_round_trips_through_ledger(self, ledger):
        spec = make_spec()
        job_id = ledger.create_job(spec)
        pending = ledger.next_pending_job()
        assert pending is not None
        found_id, found_spec = pending
        assert found_id == job_id
        assert found_spec == spec

    def test_running_and_completion(self, ledger):
        job_id = ledger.create_job(make_spec())
        ledger.mark_job_running(job_id)
        job = ledger.get_job(job_id)
        assert job["state"] == "RUNNING"
        assert job["started_at"] is not None
        assert job["heartbeat_at"] is not None
        ledger.set_job_state(job_id, JobState.COMPLETED)
        job = ledger.get_job(job_id)
        assert job["state"] == "COMPLETED"
        assert job["finished_at"] is not None
        assert ledger.next_pending_job() is None

    def test_fault_records_reason(self, ledger):
        job_id = ledger.create_job(make_spec())
        ledger.set_job_state(job_id, JobState.FAULTED, "safety: over-temp")
        assert ledger.get_job(job_id)["fault_reason"] == "safety: over-temp"

    def test_next_pending_is_fifo(self, ledger):
        first = ledger.create_job(make_spec())
        ledger.create_job(make_spec())
        found_id, _ = ledger.next_pending_job()
        assert found_id == first


class TestPhaseTracking:
    def test_phase_state_transitions_stamp_times(self, ledger):
        job_id = ledger.create_job(make_spec())
        ledger.set_phase_state(job_id, 0, PhaseState.RUNNING, session_id=7)
        phase = ledger.get_phases(job_id)[0]
        assert phase["state"] == "RUNNING"
        assert phase["started_at"] is not None
        assert phase["session_id"] == 7
        result = PhaseResult(PhaseState.COMPLETED, reason="voltage_cutoff")
        ledger.set_phase_state(job_id, 0, PhaseState.COMPLETED, result=result)
        phase = ledger.get_phases(job_id)[0]
        assert phase["state"] == "COMPLETED"
        assert phase["finished_at"] is not None
        assert "voltage_cutoff" in phase["result_json"]

    def test_set_current_phase(self, ledger):
        job_id = ledger.create_job(make_spec())
        ledger.set_current_phase(job_id, 1)
        assert ledger.get_job(job_id)["current_phase_index"] == 1

    def test_phase_row_id(self, ledger):
        job_id = ledger.create_job(make_spec())
        row_id = ledger.phase_row_id(job_id, 1)
        assert isinstance(row_id, int)
        assert ledger.phase_row_id(job_id, 99) is None


class TestOrphans:
    def test_find_orphans_sees_all_nonterminal_states(self, ledger):
        running = ledger.create_job(make_spec())
        ledger.mark_job_running(running)
        pending = ledger.create_job(make_spec())
        done = ledger.create_job(make_spec())
        ledger.set_job_state(done, JobState.COMPLETED)
        orphan_ids = {o["id"] for o in ledger.find_orphans()}
        assert orphan_ids == {running, pending}

    def test_finalize_interrupted(self, ledger):
        job_id = ledger.create_job(make_spec())
        ledger.mark_job_running(job_id)
        ledger.set_phase_state(job_id, 0, PhaseState.RUNNING)
        ledger.finalize_interrupted(job_id, "orphaned at startup (last heartbeat x)")
        job = ledger.get_job(job_id)
        assert job["state"] == "INTERRUPTED"
        assert "orphaned" in job["fault_reason"]
        states = [p["state"] for p in ledger.get_phases(job_id)]
        assert states == ["INTERRUPTED", "INTERRUPTED"]
