"""Tests for the job/phase data model."""

from load_test_bench.jobs.model import (
    TERMINAL_JOB_STATES,
    JobSpec,
    JobState,
    PhaseResult,
    PhaseSpec,
    PhaseState,
)


class TestJobSpec:
    def test_json_round_trip(self):
        """A JobSpec survives to_json/from_json unchanged."""
        spec = JobSpec(
            name="cycle test",
            job_type="cycle_test",
            phases=(
                PhaseSpec("discharge", {"current_a": 1.0, "voltage_cutoff": 3.0}),
                PhaseSpec("rest", {"duration_s": 60}),
            ),
            battery_name="18650-A",
            notes="bench 1",
            metadata={"project": "converter-burn-in"},
        )
        restored = JobSpec.from_json(spec.to_json())
        assert restored == spec
        assert isinstance(restored.phases, tuple)
        assert isinstance(restored.phases[0], PhaseSpec)

    def test_defaults(self):
        spec = JobSpec(name="d", job_type="discharge", phases=(PhaseSpec("rest", {}),))
        assert spec.battery_name == ""
        assert spec.metadata == {}

    def test_specs_are_immutable(self):
        import dataclasses
        import pytest

        spec = JobSpec(name="d", job_type="discharge", phases=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "x"


class TestStates:
    def test_terminal_states(self):
        """Terminal set covers every way a job can end and nothing live."""
        assert TERMINAL_JOB_STATES == {
            JobState.COMPLETED,
            JobState.STOPPED,
            JobState.FAULTED,
            JobState.INTERRUPTED,
        }
        assert JobState.RUNNING not in TERMINAL_JOB_STATES

    def test_states_serialize_as_strings(self):
        """Ledger rows store state names verbatim."""
        assert JobState.RUNNING.value == "RUNNING"
        assert PhaseState.INTERRUPTED.value == "INTERRUPTED"


class TestPhaseResult:
    def test_result_json(self):
        result = PhaseResult(
            state=PhaseState.COMPLETED,
            reason="voltage_cutoff",
            metrics={"capacity_mah": 2500.0},
        )
        import json

        data = json.loads(result.to_json())
        assert data == {
            "state": "COMPLETED",
            "reason": "voltage_cutoff",
            "metrics": {"capacity_mah": 2500.0},
        }
