"""Startup crash recovery: detect orphaned runs and make hardware safe.

Recovery NEVER resumes a run (battery state changes irreversibly - resumed
data would be dishonest). It finalizes the ledger/sessions with data intact
and best-effort turns outputs off.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from ..data.database import Database
from .ledger import JobLedger


@dataclass
class RecoveryReport:
    orphaned_jobs: list = field(default_factory=list)
    orphaned_session_ids: list = field(default_factory=list)
    load_off_confirmed: Optional[bool] = None  # None = not attempted
    psu_off_confirmed: Optional[bool] = None

    @property
    def found_anything(self) -> bool:
        return bool(self.orphaned_jobs or self.orphaned_session_ids)


def finalize_orphans(ledger: JobLedger, database: Database) -> RecoveryReport:
    """Mark orphaned jobs/sessions INTERRUPTED. Database-only; no hardware."""
    report = RecoveryReport()
    for job in ledger.find_orphans():
        heartbeat = job.get("heartbeat_at") or "never"
        ledger.finalize_interrupted(
            job["id"], f"orphaned at startup (last heartbeat {heartbeat})"
        )
        report.orphaned_jobs.append(job)
    for session_id in database.find_open_session_ids():
        database.close_session_as_interrupted(session_id)
        report.orphaned_session_ids.append(session_id)
    return report


def make_safe(load=None, psu=None, retries: int = 3, delay_s: float = 1.0, sleep=time.sleep):
    """Force outputs off with retries. Returns (load_off, psu_off);
    True = confirmed off, False = could NOT be confirmed off, None = not attempted.

    Load first: an electronic load left on drains the battery under test;
    a PSU left on keeps charging it - both matter, load is cheaper to stop.
    """

    def attempt(action) -> bool:
        for attempt_index in range(retries):
            if action():
                return True
            if attempt_index < retries - 1:
                sleep(delay_s)
        return False

    load_ok: Optional[bool] = None
    psu_ok: Optional[bool] = None
    if load is not None and load.is_connected:
        load_ok = attempt(load.turn_off)
    if psu is not None and psu.is_connected:
        psu_ok = attempt(psu.output_off)
    return load_ok, psu_ok
