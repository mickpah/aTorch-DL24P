"""Tests for startup detect-and-make-safe recovery."""

import pytest

from load_test_bench.data.database import Database
from load_test_bench.jobs.ledger import JobLedger
from load_test_bench.jobs.model import JobSpec, PhaseSpec
from load_test_bench.jobs.recovery import RecoveryReport, finalize_orphans, make_safe
from tests.fakes import FakeLoad, FakePsu


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "tests.db")
    yield database
    database.close()


def seed_orphan(db):
    """Simulate a crash: a RUNNING job plus an unfinalized session."""
    ledger = JobLedger(db)
    job_id = ledger.create_job(
        JobSpec(name="crashed", job_type="discharge",
                phases=(PhaseSpec("discharge", {"current_a": 1.0}),))
    )
    ledger.mark_job_running(job_id)
    db._conn.execute(
        "INSERT INTO sessions (name, start_time) VALUES ('orphan', '2026-01-01T00:00:00')"
    )
    db._conn.commit()
    return ledger, job_id


class TestFinalizeOrphans:
    def test_clean_database_reports_nothing(self, db):
        report = finalize_orphans(JobLedger(db), db)
        assert report.found_anything is False

    def test_orphans_are_finalized_with_data_intact(self, db):
        ledger, job_id = seed_orphan(db)
        report = finalize_orphans(ledger, db)
        assert report.found_anything is True
        assert [j["id"] for j in report.orphaned_jobs] == [job_id]
        assert len(report.orphaned_session_ids) == 1
        job = ledger.get_job(job_id)
        assert job["state"] == "INTERRUPTED"
        assert "orphaned at startup" in job["fault_reason"]
        assert db.find_open_session_ids() == []

    def test_recovery_is_idempotent(self, db):
        ledger, _ = seed_orphan(db)
        finalize_orphans(ledger, db)
        second = finalize_orphans(ledger, db)
        assert second.found_anything is False


class TestMakeSafe:
    def test_turns_both_devices_off(self):
        load, psu = FakeLoad(), FakePsu()
        load.on = True
        psu.output_on_state = True
        result = make_safe(load=load, psu=psu, sleep=lambda s: None)
        assert result == (True, True)
        assert load.on is False
        assert psu.output_on_state is False

    def test_retries_transient_failures(self):
        """A lock-busy first attempt succeeds on retry."""
        load = FakeLoad()
        load.fail_commands = 1
        sleeps = []
        result = make_safe(load=load, sleep=sleeps.append)
        assert result == (True, None)
        assert sleeps == [1.0]

    def test_reports_unconfirmed_after_exhausted_retries(self):
        load = FakeLoad()
        load.fail_commands = 99
        result = make_safe(load=load, retries=3, sleep=lambda s: None)
        assert result == (False, None)
        assert load.calls.count(("turn_off",)) == 3

    def test_skips_absent_or_disconnected_devices(self):
        psu = FakePsu()
        psu.connected = False
        assert make_safe(load=None, psu=psu, sleep=lambda s: None) == (None, None)


class TestRecoveryReport:
    def test_found_anything(self):
        assert RecoveryReport().found_anything is False
        assert RecoveryReport(orphaned_session_ids=[1]).found_anything is True
