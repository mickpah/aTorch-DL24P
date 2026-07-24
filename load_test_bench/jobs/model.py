"""Job and phase data model: states, specs, results, snapshots.

Everything here is JSON-serializable (the Prefect seam requirement) and
immutable where it represents intent (specs) rather than progress.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class JobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"
    INTERRUPTED = "INTERRUPTED"


class PhaseState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAULTED = "FAULTED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.STOPPED, JobState.FAULTED, JobState.INTERRUPTED}
)


@dataclass(frozen=True)
class PhaseSpec:
    """One declarative phase: a type name plus JSON-serializable params."""

    phase_type: str  # "discharge" | "rest" | "timed" | "stepped" | ("charge" in Stage 2)
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    """A declarative job: an ordered tuple of phases plus metadata.

    Cycles are expanded at submit time (discharge, rest, ... repeated) so that
    phase_index is stable and the ledger is row-per-phase.
    """

    name: str
    job_type: str
    phases: tuple = ()
    battery_name: str = ""  # kept for sessions-table compatibility
    notes: str = ""
    metadata: dict = field(default_factory=dict)  # domain-specific, opaque

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "job_type": self.job_type,
                "phases": [
                    {"phase_type": p.phase_type, "params": p.params}
                    for p in self.phases
                ],
                "battery_name": self.battery_name,
                "notes": self.notes,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "JobSpec":
        data = json.loads(text)
        return cls(
            name=data["name"],
            job_type=data["job_type"],
            phases=tuple(
                PhaseSpec(p["phase_type"], p.get("params", {}))
                for p in data.get("phases", [])
            ),
            battery_name=data.get("battery_name", ""),
            notes=data.get("notes", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class PhaseResult:
    """What a finished phase reports - the future Prefect task return value."""

    state: PhaseState
    reason: str = ""  # "voltage_cutoff", "timeout", "device_stopped", "safety_trip", ...
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"state": self.state.value, "reason": self.reason, "metrics": self.metrics}
        )


@dataclass
class JobSnapshot:
    """Point-in-time view of the active job, pushed to GUI callbacks."""

    job_id: int
    state: JobState
    spec: JobSpec
    phase_index: int
    phase_state: PhaseState
    elapsed_s: float
    message: str = ""
    fault_reason: str = ""
