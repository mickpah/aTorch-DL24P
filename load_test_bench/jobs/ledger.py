"""SQLite persistence for jobs and phases (the run ledger).

Owns all SQL touching the jobs/job_phases tables. Uses the shared Database
connection (check_same_thread=False, same pattern as the readings writer).
Every mutation commits immediately: ledger rows are only useful if durable.
"""

from datetime import datetime
from typing import Optional

from ..data.database import Database
from .model import JobSpec, JobState, PhaseResult, PhaseState

_TERMINAL_PHASE_STATES = ("COMPLETED", "SKIPPED", "FAULTED", "INTERRUPTED")


class JobLedger:
    def __init__(self, database: Database) -> None:
        self._db = database
        self._conn = database._conn

    def create_job(self, spec: JobSpec) -> int:
        now = datetime.now().isoformat()
        cursor = self._conn.cursor()
        cursor.execute(
            """INSERT INTO jobs
               (created_at, state, job_type, name, spec_json, battery_name, notes)
               VALUES (?, 'PENDING', ?, ?, ?, ?, ?)""",
            (now, spec.job_type, spec.name, spec.to_json(), spec.battery_name, spec.notes),
        )
        job_id = cursor.lastrowid
        for index, phase in enumerate(spec.phases):
            cursor.execute(
                """INSERT INTO job_phases (job_id, phase_index, phase_type, state)
                   VALUES (?, ?, ?, 'PENDING')""",
                (job_id, index, phase.phase_type),
            )
        self._conn.commit()
        return job_id

    def mark_job_running(self, job_id: int) -> None:
        now = datetime.now().isoformat()
        self._conn.execute(
            "UPDATE jobs SET state = 'RUNNING', started_at = ?, heartbeat_at = ? WHERE id = ?",
            (now, now, job_id),
        )
        self._conn.commit()

    def set_job_state(
        self, job_id: int, state: JobState, fault_reason: Optional[str] = None
    ) -> None:
        from .model import TERMINAL_JOB_STATES

        now = datetime.now().isoformat()
        if state in TERMINAL_JOB_STATES:
            self._conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, fault_reason = COALESCE(?, fault_reason) WHERE id = ?",
                (state.value, now, fault_reason, job_id),
            )
        else:
            self._conn.execute(
                "UPDATE jobs SET state = ? WHERE id = ?", (state.value, job_id)
            )
        self._conn.commit()

    def set_current_phase(self, job_id: int, phase_index: int) -> None:
        self._conn.execute(
            "UPDATE jobs SET current_phase_index = ? WHERE id = ?",
            (phase_index, job_id),
        )
        self._conn.commit()

    def set_phase_state(
        self,
        job_id: int,
        phase_index: int,
        state: PhaseState,
        session_id: Optional[int] = None,
        result: Optional[PhaseResult] = None,
    ) -> None:
        now = datetime.now().isoformat()
        sets = ["state = ?"]
        args: list = [state.value]
        if state == PhaseState.RUNNING:
            sets.append("started_at = ?")
            args.append(now)
        if state.value in _TERMINAL_PHASE_STATES:
            sets.append("finished_at = ?")
            args.append(now)
        if session_id is not None:
            sets.append("session_id = ?")
            args.append(session_id)
        if result is not None:
            sets.append("result_json = ?")
            args.append(result.to_json())
        args.extend([job_id, phase_index])
        self._conn.execute(
            f"UPDATE job_phases SET {', '.join(sets)} WHERE job_id = ? AND phase_index = ?",
            args,
        )
        self._conn.commit()

    def heartbeat(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE id = ?",
            (datetime.now().isoformat(), job_id),
        )
        self._conn.commit()

    def next_pending_job(self) -> Optional[tuple]:
        row = self._conn.execute(
            "SELECT id, spec_json FROM jobs WHERE state = 'PENDING' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return row[0], JobSpec.from_json(row[1])

    def find_orphans(self) -> list:
        rows = self._conn.execute(
            """SELECT id, name, state, heartbeat_at FROM jobs
               WHERE state IN ('RUNNING', 'PAUSED', 'PENDING') ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def finalize_interrupted(self, job_id: int, reason: str) -> None:
        now = datetime.now().isoformat()
        self._conn.execute(
            """UPDATE jobs SET state = 'INTERRUPTED', finished_at = ?, fault_reason = ?
               WHERE id = ?""",
            (now, reason, job_id),
        )
        self._conn.execute(
            f"""UPDATE job_phases SET state = 'INTERRUPTED', finished_at = ?
                WHERE job_id = ? AND state NOT IN {_TERMINAL_PHASE_STATES!r}""",
            (now, job_id),
        )
        self._conn.commit()

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_phases(self, job_id: int) -> list:
        rows = self._conn.execute(
            "SELECT * FROM job_phases WHERE job_id = ? ORDER BY phase_index", (job_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def phase_row_id(self, job_id: int, phase_index: int) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM job_phases WHERE job_id = ? AND phase_index = ?",
            (job_id, phase_index),
        ).fetchone()
        return row[0] if row else None
