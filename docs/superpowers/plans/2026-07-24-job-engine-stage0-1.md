# Job Engine Stage 0 + Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Stages 0–1 of `docs/superpowers/specs/2026-07-24-job-engine-design.md`: a durable SQLite job ledger with startup detect-and-make-safe recovery, then a single-threaded JobEngine that replaces `TestRunner` behind a compatibility facade, with an actuating SafetySupervisor and the ScpiTransport extraction.

**Architecture:** New Qt-free package `load_test_bench/jobs/` (model → ledger → devices → recovery → cores → phases → safety → engine), a layered SCPI transport in `protocol/`, and a thin Qt bridge. One engine thread issues all device commands; phases are pure decision cores + thin actuation shells; the ledger lives in `tests.db` behind a new `PRAGMA user_version` migration framework.

**Tech Stack:** Python ≥3.10 stdlib (sqlite3, threading, dataclasses, enum, typing.Protocol), PySide6 (bridge/GUI only), pytest.

## Global Constraints

- Read the spec first: `docs/superpowers/specs/2026-07-24-job-engine-design.md`. It governs on any ambiguity.
- **No Qt imports anywhere under `load_test_bench/jobs/`** — that package is the testability and Prefect-seam boundary.
- No asyncio. No new dependencies.
- Crash recovery is **detect + make safe only** — no resume, ever.
- All device commands during jobs come from the engine thread, via the drivers' existing lock-timeout methods (`GUI_LOCK_TIMEOUT = 1.0` pattern). 3 consecutive command failures → phase FAULTED → make-safe.
- Device status callbacks run on poll threads: GUI marshalling only via Qt Signals; safety evaluation in callbacks must be pure and microsecond-cheap.
- Heartbeat every 5 s, committed directly (not via `_db_queue`); readings keep flowing through the existing `_db_queue` writer.
- Tests are pure-logic with injected clocks and hand-rolled fakes — **no unittest.mock, no GUI tests** (matching `tests/test_charge_monitor.py` style: pytest `TestXxx` classes, `test_<behavior>` methods with docstrings).
- Run everything via uv: `uv run --extra dev pytest`. 152 existing tests must stay green after every task.
- Commit messages: imperative sentence, no `feat:` prefix, ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Existing DB column names are legacy (`voltage`, `current`, `temperature_c`, `runtime_seconds`); `add_reading`/`get_readings` map them to `Reading` attribute names — do not rename columns.
- `alerts/` stays notify-only and untouched. `automation/scheduler.py` is dead code — do not touch it in this plan (deleted in Stage 5).

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `load_test_bench/data/database.py` (modify) | 1 | `user_version` migration framework, migration 1 (ledger tables + columns), session-state helpers |
| `load_test_bench/jobs/__init__.py` | 2 | Package docstring: Prefect seam contract + adoption criteria |
| `load_test_bench/jobs/model.py` | 2 | Job/phase states, `PhaseSpec`/`JobSpec`/`PhaseResult`/`JobSnapshot`, JSON round-trip |
| `load_test_bench/jobs/ledger.py` | 3 | All SQL for jobs/job_phases (scheduled_jobs CRUD deferred to Stage 5) |
| `load_test_bench/jobs/devices.py` | 4 | `LoadDevice`/`PsuDevice`/`MeterDevice` Protocols, `MeterStatus`, `DeviceRegistry` |
| `tests/fakes.py` | 4 | `FakeLoad`, `FakePsu`, `FakeMeter` |
| `load_test_bench/jobs/recovery.py` | 5 | `RecoveryReport`, `finalize_orphans()`, `make_safe()` |
| `load_test_bench/gui/main_window.py` (modify) | 6, 13 | Stage-0 recovery hook; Stage-1 engine/registry/bridge/safety wiring |
| `load_test_bench/protocol/scpi_transport.py` | 7 | `ScpiError`, `ScpiLink` Protocol, `LanScpiLink`, `ScpiTransport` |
| `load_test_bench/protocol/rigol_dp832a.py` (modify) | 7 | Refactor onto `ScpiTransport`, public behavior unchanged |
| `load_test_bench/jobs/cores.py` | 8 | Pure decision FSMs: `DischargeCore`, `RestCore`, `TimedCore`, `SteppedCore` |
| `load_test_bench/jobs/phases.py` | 9 | `Phase` ABC, `PhaseContext`, `PhaseReporter`, 4 phase shells, `PHASE_TYPES` |
| `load_test_bench/jobs/safety.py` | 10 | `SafetyConfig`, `Trip`, `SafetyRules`, `SafetySupervisor` |
| `load_test_bench/jobs/engine.py` | 11 | `JobExecutor` (thread-free) + `JobEngine` (one thread) |
| `load_test_bench/automation/test_runner.py` (rewrite) | 12 | Compatibility facade over the engine |
| `load_test_bench/gui/job_bridge.py` | 13 | `JobEngineBridge(QObject)` — the only Qt↔jobs file |
| `load_test_bench/gui/dp832a_charger_panel.py` (modify) | 13 | Register PSU in registry; feed SafetySupervisor |

Note (spec deviation, intentional): the spec put cores and shells both in `phases.py`; this plan splits pure cores into `jobs/cores.py` to keep files focused. The spec's `jobs/scheduler.py` and full scheduled_jobs CRUD are Stage 5 — only the table is created here (migration 1) so no second migration is needed.

---

### Task 1: DB migration framework + migration 1 (job ledger schema)

**Files:**
- Modify: `load_test_bench/data/database.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Consumes: existing `Database.__init__(path: Optional[Path])`, `_init_db()` (creates v0 `sessions`/`readings` via `CREATE TABLE IF NOT EXISTS`, no pragmas today).
- Produces (later tasks rely on): tables `jobs`, `job_phases`, `scheduled_jobs`; columns `sessions.status` (TEXT NOT NULL DEFAULT 'completed'), `sessions.job_phase_id` (INTEGER), `readings.aux_voltage_v` (REAL); `PRAGMA user_version == 1`; new methods `Database.find_open_session_ids() -> list[int]`, `Database.close_session_as_interrupted(session_id: int) -> None`, `Database.set_session_status(session_id: int, status: str) -> None`, `Database.link_session_to_phase(session_id: int, job_phase_id: int) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrations.py`:

```python
"""Tests for the tests.db schema migration framework (PRAGMA user_version)."""

import sqlite3

from load_test_bench.data.database import Database

# Verbatim v0 schema, for building a pre-migration fixture database.
V0_SESSIONS = """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT,
        battery_name TEXT,
        battery_capacity_mah REAL,
        notes TEXT,
        test_type TEXT,
        settings TEXT
    )
"""
V0_READINGS = """
    CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        voltage REAL NOT NULL,
        current REAL NOT NULL,
        power REAL NOT NULL,
        energy_wh REAL NOT NULL,
        capacity_mah REAL NOT NULL,
        temperature_c INTEGER NOT NULL,
        ext_temperature_c INTEGER,
        fan_speed_rpm INTEGER DEFAULT 0,
        load_r_ohm REAL,
        battery_r_ohm REAL,
        runtime_seconds INTEGER NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions (id)
    )
"""


def make_v0_db(path):
    """Hand-build a pre-migration database as deployed installs have it."""
    conn = sqlite3.connect(str(path))
    conn.execute(V0_SESSIONS)
    conn.execute(V0_READINGS)
    conn.execute(
        "INSERT INTO sessions (name, start_time, end_time) VALUES (?, ?, ?)",
        ("finished run", "2026-01-01T10:00:00", "2026-01-01T11:00:00"),
    )
    conn.execute(
        "INSERT INTO sessions (name, start_time, end_time) VALUES (?, ?, NULL)",
        ("crashed run", "2026-01-02T10:00:00"),
    )
    conn.commit()
    conn.close()


def table_names(db):
    rows = db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in rows}


def column_names(db, table):
    return {row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")}


class TestMigrations:
    def test_fresh_database_reaches_version_1(self, tmp_path):
        """A brand-new database gets the full current schema."""
        db = Database(tmp_path / "tests.db")
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {"jobs", "job_phases", "scheduled_jobs"} <= table_names(db)
        assert {"status", "job_phase_id"} <= column_names(db, "sessions")
        assert "aux_voltage_v" in column_names(db, "readings")
        db.close()

    def test_v0_database_migrates_in_place(self, tmp_path):
        """A deployed v0 database gains the new tables/columns with data intact."""
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {"jobs", "job_phases", "scheduled_jobs"} <= table_names(db)
        rows = db._conn.execute(
            "SELECT name, status FROM sessions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("finished run", "completed"),
            ("crashed run", "completed"),  # backfill default; recovery flips orphans
        ]
        db.close()

    def test_reopening_is_idempotent(self, tmp_path):
        """Opening an already-migrated database runs no migration twice."""
        path = tmp_path / "tests.db"
        Database(path).close()
        db = Database(path)
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        db.close()


class TestSessionStateHelpers:
    def test_find_open_session_ids(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        open_ids = db.find_open_session_ids()
        assert len(open_ids) == 1
        db.close()

    def test_close_session_as_interrupted_uses_last_reading_time(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        conn = sqlite3.connect(str(path))
        conn.execute(
            """INSERT INTO readings (session_id, timestamp, voltage, current, power,
               energy_wh, capacity_mah, temperature_c, runtime_seconds)
               VALUES (2, '2026-01-02T10:30:00', 3.7, 1.0, 3.7, 1.0, 500, 30, 1800)"""
        )
        conn.commit()
        conn.close()
        db = Database(path)
        (session_id,) = db.find_open_session_ids()
        db.close_session_as_interrupted(session_id)
        row = db._conn.execute(
            "SELECT end_time, status FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "2026-01-02T10:30:00"
        assert row[1] == "interrupted"
        assert db.find_open_session_ids() == []
        db.close()

    def test_close_session_without_readings_falls_back_to_start_time(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        (session_id,) = db.find_open_session_ids()
        db.close_session_as_interrupted(session_id)
        row = db._conn.execute(
            "SELECT end_time FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "2026-01-02T10:00:00"
        db.close()

    def test_set_status_and_link_phase(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        db._conn.execute(
            "INSERT INTO sessions (name, start_time) VALUES ('s', '2026-01-01T00:00:00')"
        )
        db._conn.commit()
        db.set_session_status(1, "faulted")
        db.link_session_to_phase(1, 42)
        row = db._conn.execute(
            "SELECT status, job_phase_id FROM sessions WHERE id = 1"
        ).fetchone()
        assert tuple(row) == ("faulted", 42)
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_migrations.py -v`
Expected: FAIL — fresh DB has `user_version == 0`, `jobs` table missing, and `Database` has no `find_open_session_ids`.

- [ ] **Step 3: Implement migrations in `database.py`**

At module level in `load_test_bench/data/database.py` (after the imports, before `class Database`), add:

```python
def _migration_1_job_ledger(conn: sqlite3.Connection) -> None:
    """Job ledger tables and session/reading state columns.

    First installment of the Database Schema Overhaul (see
    docs/superpowers/specs/2026-07-24-job-engine-design.md).
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            state TEXT NOT NULL DEFAULT 'PENDING',
            job_type TEXT NOT NULL,
            name TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            current_phase_index INTEGER NOT NULL DEFAULT 0,
            heartbeat_at TEXT,
            fault_reason TEXT,
            schedule_id INTEGER REFERENCES scheduled_jobs(id),
            battery_name TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE job_phases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            phase_index INTEGER NOT NULL,
            phase_type TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'PENDING',
            started_at TEXT,
            finished_at TEXT,
            session_id INTEGER REFERENCES sessions(id),
            result_json TEXT,
            UNIQUE (job_id, phase_index)
        )
        """
    )
    cursor.execute("CREATE INDEX idx_job_phases_job ON job_phases(job_id)")
    cursor.execute(
        """
        CREATE TABLE scheduled_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT NOT NULL,
            repeat_interval_s INTEGER,
            grace_window_s INTEGER NOT NULL DEFAULT 3600,
            spec_json TEXT NOT NULL,
            last_run_job_id INTEGER REFERENCES jobs(id)
        )
        """
    )
    cursor.execute(
        "ALTER TABLE sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
    )
    cursor.execute("ALTER TABLE sessions ADD COLUMN job_phase_id INTEGER")
    cursor.execute("ALTER TABLE readings ADD COLUMN aux_voltage_v REAL")


_MIGRATIONS = [_migration_1_job_ledger]
```

In `Database._init_db`, after the existing base-table creation and its `commit()`, add a call `self._run_migrations()`, and add the method:

```python
def _run_migrations(self) -> None:
    """Apply pending schema migrations, tracked via PRAGMA user_version."""
    version = self._conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(_MIGRATIONS)):
        _MIGRATIONS[index](self._conn)
        self._conn.execute(f"PRAGMA user_version = {index + 1}")
        self._conn.commit()
```

Add the four session-state helpers to `Database` (near `update_session`):

```python
def find_open_session_ids(self) -> list[int]:
    """Sessions never finalized (end_time NULL) - crash orphans."""
    cursor = self._conn.execute("SELECT id FROM sessions WHERE end_time IS NULL")
    return [row[0] for row in cursor.fetchall()]

def close_session_as_interrupted(self, session_id: int) -> None:
    """Finalize an orphaned session: end_time = last reading (or start_time)."""
    cursor = self._conn.cursor()
    cursor.execute(
        "SELECT MAX(timestamp) FROM readings WHERE session_id = ?", (session_id,)
    )
    end_time = cursor.fetchone()[0]
    if end_time is None:
        cursor.execute("SELECT start_time FROM sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        end_time = row[0] if row else datetime.now().isoformat()
    cursor.execute(
        "UPDATE sessions SET end_time = ?, status = 'interrupted' WHERE id = ?",
        (end_time, session_id),
    )
    self._conn.commit()

def set_session_status(self, session_id: int, status: str) -> None:
    self._conn.execute(
        "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
    )
    self._conn.commit()

def link_session_to_phase(self, session_id: int, job_phase_id: int) -> None:
    self._conn.execute(
        "UPDATE sessions SET job_phase_id = ? WHERE id = ?",
        (job_phase_id, session_id),
    )
    self._conn.commit()
```

(`datetime` is already imported in `database.py`; verify and add if not.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_migrations.py -v`
Expected: all PASS (8 tests)

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: 152 pre-existing + 8 new, all PASS (existing `test_database.py` must be unaffected — it constructs `Database(tmp_path)` and now silently gets the v1 schema).

- [ ] **Step 6: Commit**

```bash
git add load_test_bench/data/database.py tests/test_migrations.py
git commit -m "Add schema migration framework and job ledger tables"
```

---

### Task 2: Job model (`jobs/model.py`)

**Files:**
- Create: `load_test_bench/jobs/__init__.py`
- Create: `load_test_bench/jobs/model.py`
- Test: `tests/test_job_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `JobState` / `PhaseState` (str-valued Enums), `TERMINAL_JOB_STATES: frozenset[JobState]`, `PhaseSpec(phase_type: str, params: dict)` (frozen), `JobSpec(name, job_type, phases: tuple[PhaseSpec, ...], battery_name="", notes="", metadata={})` (frozen) with `to_json() -> str` and classmethod `from_json(text: str) -> JobSpec`, `PhaseResult(state: PhaseState, reason: str = "", metrics: dict = {})` with `to_json()`, `JobSnapshot(job_id, state, spec, phase_index, phase_state, elapsed_s, message="", fault_reason="")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_job_model.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_job_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs'`

- [ ] **Step 3: Implement**

Create `load_test_bench/jobs/__init__.py`:

```python
"""Durable job engine for test orchestration.

This package is deliberately Qt-free: it is both the testability boundary and
the Prefect seam. Phases take JSON-serializable params plus an injected
PhaseContext, return a JSON-serializable PhaseResult, and report progress only
through PhaseReporter - so a later orchestrator can wrap a phase as a task
without rewriting it.

Prefect adoption criteria (evaluated 2026-07-24, decision: not now): adopt
only when (a) the rig goes headless and the Qt UI stops being the operator
interface, (b) more than one rig needs central scheduling/observability, or
(c) cross-machine retry/caching/artifact semantics are needed. The
SafetySupervisor stays app-side under every future architecture - an
orchestrator must never be in the emergency-stop path.
"""
```

Create `load_test_bench/jobs/model.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_job_model.py -v`
Expected: all PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/__init__.py load_test_bench/jobs/model.py tests/test_job_model.py
git commit -m "Add job engine package with job and phase data model"
```

---

### Task 3: Job ledger (`jobs/ledger.py`)

**Files:**
- Create: `load_test_bench/jobs/ledger.py`
- Test: `tests/test_job_ledger.py`

**Interfaces:**
- Consumes: Task 1 tables/columns; Task 2 `JobSpec`, `JobState`, `PhaseState`, `PhaseResult`.
- Produces: `JobLedger(database: Database)` with:
  - `create_job(spec: JobSpec) -> int` — jobs row PENDING + one job_phases row per phase, committed
  - `mark_job_running(job_id: int) -> None` — state RUNNING, `started_at`, first heartbeat
  - `set_job_state(job_id: int, state: JobState, fault_reason: Optional[str] = None) -> None` — terminal states also set `finished_at`
  - `set_current_phase(job_id: int, phase_index: int) -> None`
  - `set_phase_state(job_id: int, phase_index: int, state: PhaseState, session_id: Optional[int] = None, result: Optional[PhaseResult] = None) -> None` — RUNNING sets `started_at`, terminal sets `finished_at`
  - `heartbeat(job_id: int) -> None`
  - `next_pending_job() -> Optional[tuple[int, JobSpec]]` — lowest-id PENDING
  - `find_orphans() -> list[dict]` — jobs in RUNNING/PAUSED/PENDING (dicts with `id`, `name`, `state`, `heartbeat_at`)
  - `finalize_interrupted(job_id: int, reason: str) -> None` — job → INTERRUPTED (+ fault_reason), non-terminal phases → INTERRUPTED
  - `get_job(job_id: int) -> Optional[dict]`, `get_phases(job_id: int) -> list[dict]`
  - `phase_row_id(job_id: int, phase_index: int) -> Optional[int]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_job_ledger.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_job_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.ledger'`

- [ ] **Step 3: Implement**

Create `load_test_bench/jobs/ledger.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_job_ledger.py -v`
Expected: all PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/ledger.py tests/test_job_ledger.py
git commit -m "Add SQLite job ledger for durable run state"
```

---

### Task 4: Device protocols, registry, and fakes (`jobs/devices.py`, `tests/fakes.py`)

**Files:**
- Create: `load_test_bench/jobs/devices.py`
- Create: `tests/fakes.py`
- Test: `tests/test_devices.py`

**Interfaces:**
- Consumes: `USBHIDDevice` (`protocol/device.py`) and `RigolDP832A` (`protocol/rigol_dp832a.py`) — their real method names (verified): load: `turn_on()`, `turn_off()`, `set_mode(mode, value=None)`, `set_current(current_a)`, `set_resistance(resistance_ohm)`, `set_voltage_cutoff(voltage)`, `reset_counters()`, `is_connected`, `last_status`; psu: `set_voltage(volts)`, `set_current(amps)`, `set_ovp(volts)`, `output_on()`, `output_off()`, `is_connected`, `last_status`.
- Produces: `@runtime_checkable` Protocols `LoadDevice`, `PsuDevice`, `MeterDevice`; `MeterStatus(voltage_v: float, current_a: Optional[float] = None)`; `DeviceRegistry` with `register(role: str, device) -> None`, `unregister(role: str) -> None`, `get(role: str) -> Optional[object]`, properties `load`/`psu`/`meter`, `ROLES = ("load", "psu", "meter")`; fakes `FakeLoad`, `FakePsu`, `FakeMeter` (in `tests/fakes.py`, importable as `from tests.fakes import FakeLoad, ...`).
- Fake behavior contract (Tasks 5, 9, 11, 12 rely on this): each fake records every call in `self.calls` (list of tuples like `("set_current", 1.0)`), has `self.status` (settable, returned by `last_status`), `self.connected = True` (returned by `is_connected`), tracks `self.on` (load) / `self.output_on_state` (psu), and has `self.fail_commands = 0` — while > 0, every command method returns False and decrements it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_devices.py`:

```python
"""Tests for device role protocols and the registry."""

import pytest

from load_test_bench.jobs.devices import (
    DeviceRegistry,
    LoadDevice,
    MeterDevice,
    MeterStatus,
    PsuDevice,
)
from load_test_bench.protocol.device import USBHIDDevice
from load_test_bench.protocol.rigol_dp832a import RigolDP832A
from tests.fakes import FakeLoad, FakeMeter, FakePsu


class TestProtocolConformance:
    def test_usbhid_device_is_a_load_device(self):
        """The real DL24 driver satisfies the LoadDevice protocol."""
        assert isinstance(USBHIDDevice(), LoadDevice)

    def test_rigol_is_a_psu_device(self):
        assert isinstance(RigolDP832A(), PsuDevice)

    def test_fakes_conform(self):
        assert isinstance(FakeLoad(), LoadDevice)
        assert isinstance(FakePsu(), PsuDevice)
        assert isinstance(FakeMeter(), MeterDevice)


class TestFakeBehavior:
    def test_fake_load_records_calls_and_state(self):
        load = FakeLoad()
        assert load.turn_on() is True
        assert load.on is True
        assert load.set_current(1.5) is True
        assert ("set_current", 1.5) in load.calls
        load.turn_off()
        assert load.on is False

    def test_fake_command_failure_injection(self):
        load = FakeLoad()
        load.fail_commands = 2
        assert load.turn_on() is False
        assert load.set_current(1.0) is False
        assert load.turn_on() is True

    def test_fake_psu_output_state(self):
        psu = FakePsu()
        psu.output_on()
        assert psu.output_on_state is True
        psu.output_off()
        assert psu.output_on_state is False


class TestDeviceRegistry:
    def test_register_get_unregister(self):
        registry = DeviceRegistry()
        load = FakeLoad()
        registry.register("load", load)
        assert registry.load is load
        assert registry.get("load") is load
        registry.unregister("load")
        assert registry.load is None

    def test_unknown_role_rejected(self):
        registry = DeviceRegistry()
        with pytest.raises(ValueError):
            registry.register("oscilloscope", FakeLoad())

    def test_meter_status_fields(self):
        status = MeterStatus(voltage_v=4.19)
        assert status.voltage_v == 4.19
        assert status.current_a is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_devices.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.devices'`

- [ ] **Step 3: Implement `jobs/devices.py`**

```python
"""Device role protocols and the thread-safe device registry.

Roles, not concrete drivers: anything satisfying LoadDevice can be the load
(DL24 over USB HID today, a SCPI electronic load later). reset_counters and
device-side accumulators are DL24 conveniences - treat them as optional
capabilities; the engine integrates capacity in software when absent.
"""

import threading
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class LoadDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[object]: ...
    def set_mode(self, mode: int, value: Optional[float] = None) -> bool: ...
    def set_current(self, current_a: float) -> bool: ...
    def set_resistance(self, resistance_ohm: float) -> bool: ...
    def set_voltage_cutoff(self, voltage: float) -> bool: ...
    def reset_counters(self) -> bool: ...
    def turn_on(self) -> bool: ...
    def turn_off(self) -> bool: ...


@runtime_checkable
class PsuDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[object]: ...
    def set_voltage(self, volts: float) -> bool: ...
    def set_current(self, amps: float) -> bool: ...
    def set_ovp(self, volts: float) -> bool: ...
    def output_on(self) -> bool: ...
    def output_off(self) -> bool: ...


@dataclass
class MeterStatus:
    """Snapshot from an independent measurement instrument (e.g. SCPI DMM)."""

    voltage_v: float
    current_a: Optional[float] = None


@runtime_checkable
class MeterDevice(Protocol):
    @property
    def is_connected(self) -> bool: ...
    @property
    def last_status(self) -> Optional[MeterStatus]: ...


class DeviceRegistry:
    """Currently connected device per role. Thread-safe; ownership stays
    with whoever connected the device (MainWindow / panels)."""

    ROLES = ("load", "psu", "meter")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict = {}

    def register(self, role: str, device: object) -> None:
        if role not in self.ROLES:
            raise ValueError(f"Unknown device role: {role}")
        with self._lock:
            self._devices[role] = device

    def unregister(self, role: str) -> None:
        with self._lock:
            self._devices.pop(role, None)

    def get(self, role: str) -> Optional[object]:
        with self._lock:
            return self._devices.get(role)

    @property
    def load(self):
        return self.get("load")

    @property
    def psu(self):
        return self.get("psu")

    @property
    def meter(self):
        return self.get("meter")
```

- [ ] **Step 4: Implement `tests/fakes.py`**

```python
"""Hand-rolled fake devices for job engine tests (house style: no mock lib).

Contract used across test files: every command is recorded in .calls, .status
is returned by last_status, .connected by is_connected, and while
fail_commands > 0 every command returns False and decrements it.
"""

from typing import Optional

from load_test_bench.jobs.devices import MeterStatus


class _FakeBase:
    def __init__(self) -> None:
        self.calls: list = []
        self.status = None
        self.connected = True
        self.fail_commands = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def last_status(self):
        return self.status

    def _command(self, name: str, *args) -> bool:
        self.calls.append((name, *args))
        if self.fail_commands > 0:
            self.fail_commands -= 1
            return False
        return True


class FakeLoad(_FakeBase):
    def __init__(self) -> None:
        super().__init__()
        self.on = False

    def set_mode(self, mode: int, value: Optional[float] = None) -> bool:
        return self._command("set_mode", mode, value)

    def set_current(self, current_a: float) -> bool:
        return self._command("set_current", current_a)

    def set_resistance(self, resistance_ohm: float) -> bool:
        return self._command("set_resistance", resistance_ohm)

    def set_voltage_cutoff(self, voltage: float) -> bool:
        return self._command("set_voltage_cutoff", voltage)

    def reset_counters(self) -> bool:
        return self._command("reset_counters")

    def turn_on(self) -> bool:
        ok = self._command("turn_on")
        if ok:
            self.on = True
        return ok

    def turn_off(self) -> bool:
        ok = self._command("turn_off")
        if ok:
            self.on = False
        return ok


class FakePsu(_FakeBase):
    def __init__(self) -> None:
        super().__init__()
        self.output_on_state = False

    def set_voltage(self, volts: float) -> bool:
        return self._command("set_voltage", volts)

    def set_current(self, amps: float) -> bool:
        return self._command("set_current", amps)

    def set_ovp(self, volts: float) -> bool:
        return self._command("set_ovp", volts)

    def output_on(self) -> bool:
        ok = self._command("output_on")
        if ok:
            self.output_on_state = True
        return ok

    def output_off(self) -> bool:
        ok = self._command("output_off")
        if ok:
            self.output_on_state = False
        return ok


class FakeMeter(_FakeBase):
    def __init__(self, voltage_v: float = 0.0) -> None:
        super().__init__()
        self.status = MeterStatus(voltage_v=voltage_v)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_devices.py -v`
Expected: all PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add load_test_bench/jobs/devices.py tests/fakes.py tests/test_devices.py
git commit -m "Add device role protocols, registry, and test fakes"
```

---

### Task 5: Startup recovery logic (`jobs/recovery.py`)

**Files:**
- Create: `load_test_bench/jobs/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Consumes: Task 3 `JobLedger.find_orphans()/finalize_interrupted()`; Task 1 `Database.find_open_session_ids()/close_session_as_interrupted()`; Task 4 fakes.
- Produces:
  - `RecoveryReport(orphaned_jobs: list[dict], orphaned_session_ids: list[int], load_off_confirmed: Optional[bool] = None, psu_off_confirmed: Optional[bool] = None)` with property `found_anything -> bool`
  - `finalize_orphans(ledger: JobLedger, database: Database) -> RecoveryReport` — DB-only, no hardware
  - `make_safe(load=None, psu=None, retries: int = 3, delay_s: float = 1.0, sleep=time.sleep) -> tuple[Optional[bool], Optional[bool]]` — `(load_off_confirmed, psu_off_confirmed)`, `None` = not attempted (device absent or not connected). Task 11 reuses `make_safe` for safety trips and phase faults.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_recovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.recovery'`

- [ ] **Step 3: Implement `jobs/recovery.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_recovery.py -v`
Expected: all PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/recovery.py tests/test_recovery.py
git commit -m "Add startup orphan finalization and make-safe routine"
```

---

### Task 6: Stage-0 MainWindow wiring — recovery at startup

**Files:**
- Modify: `load_test_bench/gui/main_window.py`

**Interfaces:**
- Consumes: Task 3 `JobLedger`, Task 5 `finalize_orphans`/`make_safe`, existing `USBHIDDevice` (already imported in main_window), `RigolDP832A`, `get_data_dir` (already imported).
- Produces: `MainWindow.job_ledger` attribute (Task 11/13 reuse it); a class-level Qt signal `recovery_safe_result = Signal(str)`.

No unit tests (GUI, per house style) — verification is a seeded-orphan smoke test.

- [ ] **Step 1: Add imports and signal**

In `load_test_bench/gui/main_window.py`, add to the import block (after `from ..protocol.atorch_protocol import DeviceStatus`):

```python
from ..protocol.rigol_dp832a import RigolDP832A
from ..jobs.ledger import JobLedger
from ..jobs.recovery import finalize_orphans, make_safe
```

Ensure `import threading` is present at the top (add it after `import sys` if missing — `json` is already imported).

In the class-level signal block (directly after `prepare_needed = Signal()  # ...`):

```python
    recovery_safe_result = Signal(str)  # startup make-safe outcome for the statusbar
```

- [ ] **Step 2: Hook recovery into `__init__`**

Find `self._create_statusbar()` in `__init__` and insert directly after it:

```python
        # Startup crash recovery: detect orphaned runs, make hardware safe.
        # Never resumes anything - see jobs/recovery.py and the design spec.
        self.job_ledger = JobLedger(self.database)
        self.recovery_safe_result.connect(self.statusbar.showMessage)
        self._run_startup_recovery()
```

- [ ] **Step 3: Add the recovery methods**

Add to `MainWindow` (near the end of the class, before `closeEvent`):

```python
    def _run_startup_recovery(self) -> None:
        """Finalize runs orphaned by a crash; kick off background make-safe."""
        report = finalize_orphans(self.job_ledger, self.database)
        if not report.found_anything:
            return
        job_lines = [
            f"  • job {job['id']} '{job['name']}' ({job['state']}, "
            f"last heartbeat {job.get('heartbeat_at') or 'never'})"
            for job in report.orphaned_jobs
        ]
        if report.orphaned_session_ids:
            job_lines.append(
                f"  • {len(report.orphaned_session_ids)} unfinalized data session(s)"
            )
        threading.Thread(target=self._startup_make_safe, daemon=True).start()
        QMessageBox.warning(
            self,
            "Interrupted runs recovered",
            "The previous session did not shut down cleanly. These runs were "
            "finalized as interrupted (all recorded data kept):\n\n"
            + "\n".join(job_lines)
            + "\n\nAttempting to turn device outputs off in the background - "
            "verify on the instruments. No run is resumed.",
        )

    def _startup_make_safe(self) -> None:
        """Best-effort outputs-off on a background thread (startup never blocks).

        Emits the outcome via recovery_safe_result - this runs OFF the GUI
        thread, so no direct widget access here.
        """
        load_ok = psu_ok = None
        try:
            if USBHIDDevice.is_available():
                dl24 = USBHIDDevice()
                try:
                    if dl24.connect():
                        load_ok, _ = make_safe(load=dl24)
                finally:
                    dl24.disconnect()
        except Exception:
            load_ok = False
        try:
            session_file = get_data_dir() / "sessions" / "dp832a_charger_session.json"
            if session_file.exists():
                with open(session_file) as f:
                    saved = json.load(f)
                host = (saved.get("host") or "").strip()
                if host:
                    psu = RigolDP832A()
                    try:
                        psu.set_channel(saved.get("channel", 1))
                        psu.connect(host, saved.get("port", RigolDP832A.DEFAULT_PORT))
                        _, psu_ok = make_safe(psu=psu)
                    finally:
                        psu.disconnect()
        except Exception:
            psu_ok = False

        def describe(name: str, ok) -> str:
            if ok is None:
                return f"{name}: not present"
            if ok:
                return f"{name}: output confirmed OFF"
            return f"{name}: OUTPUT STATE UNKNOWN - check the instrument"

        self.recovery_safe_result.emit(
            "Recovery make-safe - "
            + describe("DL24", load_ok)
            + "; "
            + describe("DP832A", psu_ok)
        )
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: all PASS (no GUI tests exist; this confirms no import breakage).

- [ ] **Step 5: Smoke test with a seeded orphan**

Seed an orphaned RUNNING job into the real data-dir database:

```bash
uv run python -c "
from load_test_bench.data.database import Database
from load_test_bench.jobs.ledger import JobLedger
from load_test_bench.jobs.model import JobSpec, PhaseSpec
db = Database(); ledger = JobLedger(db)
job_id = ledger.create_job(JobSpec(name='seeded orphan', job_type='discharge', phases=(PhaseSpec('discharge', {}),)))
ledger.mark_job_running(job_id)
db.close()
print('seeded job', job_id)"
```

Launch the app in the background (`uv run python -m load_test_bench.main` with `run_in_background`; kill any prior instance you launched first, by task ID). Expect: the "Interrupted runs recovered" warning dialog naming the seeded job, and (after dismissing) a statusbar line like `Recovery make-safe - DL24: ...; DP832A: not present`. Relaunch once more: no dialog (idempotent). Kill the app instance.

- [ ] **Step 6: Commit**

```bash
git add load_test_bench/gui/main_window.py
git commit -m "Run detect-and-make-safe crash recovery at startup"
```

---

### Task 7: ScpiTransport extraction (`protocol/scpi_transport.py`) + RigolDP832A refactor

**Files:**
- Create: `load_test_bench/protocol/scpi_transport.py`
- Modify: `load_test_bench/protocol/rigol_dp832a.py` (full rewrite below)
- Modify: `tests/test_rigol_dp832a.py` (updated to the link seam; same behavioral coverage)
- Test: `tests/test_scpi_transport.py`

**Interfaces:**
- Consumes: `DP832AProtocol`, `ChargerStatus` (unchanged).
- Produces:
  - `ScpiError(Exception)`; `ChargerError(ScpiError)` (kept in `rigol_dp832a.py` — panel `except ChargerError` clauses keep working)
  - `ScpiLink` Protocol: `open() -> None`, `close() -> None`, `send(data: bytes) -> None`, `recv(max_bytes: int) -> bytes`
  - `LanScpiLink(host: str, port: int, timeout: float = 2.0, sock=None)` — injectable pre-opened socket for tests
  - `ScpiTransport(link, poll_interval=1.0, lock_timeout=1.0)` with: properties `is_connected`/`identity`/`last_status`; `set_status_callback(cb)`, `set_error_callback(cb)`; `connect(verify_idn: Callable[[str], bool], describe: str) -> None` (raises `ScpiError`); `start_polling(poll_once: Callable[[], object]) -> None`; `disconnect()`; `command(cmd: str) -> bool` (lock-timeout, never raises on I/O); `run_locked(fn)`; `query(cmd) -> str` / `write(cmd)` (no locking — for use inside `run_locked`); `_poll_tick()` (clears `last_status` on any failure)
  - `RigolDP832A(transport: Optional[ScpiTransport] = None)` — public surface unchanged: `DEFAULT_PORT`, `connect(host, port=5555)`, `disconnect()`, `set_channel`, `set_voltage`, `set_current`, `set_ovp`, `output_on`, `output_off`, `set_status_callback`, `set_error_callback`, properties `is_connected`/`host`/`identity`/`channel`/`last_status`
  - Test seam: `FakeLink` (in `tests/test_rigol_dp832a.py`) replaces `FakeSocket`; UsbScpiLink is **deliberately deferred** with the HDS200 driver (its protocol PDF is an empty placeholder).

- [ ] **Step 1: Write the failing transport tests**

Create `tests/test_scpi_transport.py`:

```python
"""Tests for the link-agnostic SCPI transport."""

import pytest

from load_test_bench.protocol.scpi_transport import (
    LanScpiLink,
    ScpiError,
    ScpiTransport,
)


class FakeLink:
    """Scripted ScpiLink: records sent commands, replies from a table."""

    def __init__(self, responses=None):
        self.responses = {"*IDN?": "RIGOL TECHNOLOGIES,DP832A,DP8A1,00.01.16\n"}
        if responses:
            self.responses.update(responses)
        self.sent = []
        self._pending = b""
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def send(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, max_bytes):
        data, self._pending = self._pending, b""
        return data


class BrokenLink(FakeLink):
    def send(self, data):
        raise OSError("unreachable")


class UnreachableLink(FakeLink):
    def open(self):
        raise OSError("no route to host")


class TestConnect:
    def test_connect_verifies_identity(self):
        link = FakeLink()
        transport = ScpiTransport(link)
        transport.connect(lambda idn: "DP832A" in idn, describe="test instrument")
        assert transport.is_connected is True
        assert "DP832A" in transport.identity

    def test_connect_rejects_wrong_instrument(self):
        link = FakeLink({"*IDN?": "RIGOL TECHNOLOGIES,DS1054Z,X,Y\n"})
        transport = ScpiTransport(link)
        with pytest.raises(ScpiError):
            transport.connect(lambda idn: "DP832A" in idn, describe="test instrument")
        assert link.closed is True

    def test_connect_unreachable_raises(self):
        transport = ScpiTransport(UnreachableLink())
        with pytest.raises(ScpiError):
            transport.connect(lambda idn: True, describe="test instrument")


class TestCommands:
    def make_connected(self, link=None):
        transport = ScpiTransport(link if link is not None else FakeLink())
        transport._connected = True
        return transport

    def test_command_writes_terminated_line(self):
        transport = self.make_connected()
        assert transport.command(":OUTP CH1,ON") is True
        assert transport._link.sent == [":OUTP CH1,ON"]

    def test_command_failure_reports_and_returns_false(self):
        transport = self.make_connected(BrokenLink())
        errors = []
        transport.set_error_callback(errors.append)
        assert transport.command(":OUTP CH1,OFF") is False
        assert len(errors) == 1

    def test_command_lock_busy_drops_with_error(self):
        """The GUI lock-timeout path: a held lock drops the command."""
        transport = self.make_connected()
        transport._lock_timeout = 0.01
        errors = []
        transport.set_error_callback(errors.append)
        assert transport._lock.acquire()
        try:
            assert transport.command(":OUTP CH1,OFF") is False
        finally:
            transport._lock.release()
        assert len(errors) == 1
        assert transport._link.sent == []


class TestPolling:
    def test_poll_tick_stores_status(self):
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        transport._poll_fn = lambda: {"voltage": 4.1}
        transport._poll_tick()
        assert transport.last_status == {"voltage": 4.1}

    def test_poll_tick_clears_last_status_on_failure(self):
        """A failed poll must invalidate last_status - consumers must never
        mistake stale data for a fresh reading."""
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        transport._last_status = {"voltage": 4.1}
        errors = []
        transport.set_error_callback(errors.append)

        def failing_poll():
            raise OSError("gone")

        transport._poll_fn = failing_poll
        transport._poll_tick()
        assert transport.last_status is None
        assert len(errors) == 1

    def test_status_callback_fires_on_success(self):
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        seen = []
        transport.set_status_callback(seen.append)
        transport._poll_fn = lambda: "status"
        transport._poll_tick()
        assert seen == ["status"]


class TestLanScpiLink:
    def test_injected_socket_used_verbatim(self):
        class Sock:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def recv(self, n):
                return b"ok\n"

            def close(self):
                pass

            def settimeout(self, t):
                pass

        sock = Sock()
        link = LanScpiLink("h", 5555, sock=sock)
        link.open()  # no-op with injected socket
        link.send(b"*IDN?\n")
        assert sock.sent == b"*IDN?\n"
        assert link.recv(64) == b"ok\n"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_scpi_transport.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.protocol.scpi_transport'`

- [ ] **Step 3: Implement `protocol/scpi_transport.py`**

```python
"""Generic SCPI instrument transport, link-agnostic.

Two layers (see docs/superpowers/specs/2026-07-24-job-engine-design.md):
ScpiLink is a minimal byte pipe - LAN socket today (LanScpiLink), USB CDC
serial later when the HDS200 meter driver lands. ScpiTransport adds line
framing, *IDN? verification, the lock-timeout command pattern (CLAUDE.md
"Lock Timeout for GUI Operations"), and a poll thread that invalidates
last_status on failure so stale data can never be mistaken for fresh.

Device drivers (RigolDP832A, future instruments) supply only SCPI string
building/parsing plus a poll_once() that reads their status under the
transport lock. Status/error callbacks fire on the poll thread - GUI
consumers must marshal through Qt Signals.
"""

import socket
import threading
import time
from typing import Callable, Optional, Protocol


class ScpiError(Exception):
    """Raised on SCPI connection or identification failures."""


class ScpiLink(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, data: bytes) -> None: ...
    def recv(self, max_bytes: int) -> bytes: ...


class LanScpiLink:
    """SCPI over a TCP socket (e.g. Rigol DP832A, raw SCPI on port 5555).

    A pre-opened socket-like object may be injected for tests; open() is then
    a no-op.
    """

    def __init__(self, host: str, port: int, timeout: float = 2.0, sock=None) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock = sock

    def open(self) -> None:
        if self._sock is None:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
            self._sock.settimeout(self._timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise OSError("Link not open")
        self._sock.sendall(data)

    def recv(self, max_bytes: int) -> bytes:
        if self._sock is None:
            raise OSError("Link not open")
        return self._sock.recv(max_bytes)


class ScpiTransport:
    POLL_INTERVAL = 1.0  # seconds
    LOCK_TIMEOUT = 1.0  # seconds; GUI commands must never block longer

    def __init__(
        self,
        link: ScpiLink,
        poll_interval: float = POLL_INTERVAL,
        lock_timeout: float = LOCK_TIMEOUT,
    ) -> None:
        self._link = link
        self._poll_interval = poll_interval
        self._lock_timeout = lock_timeout
        self._lock = threading.Lock()
        self._connected = False
        self._identity = ""
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_fn: Optional[Callable[[], object]] = None
        self._last_status: Optional[object] = None
        self._status_callback: Optional[Callable[[object], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def last_status(self) -> Optional[object]:
        return self._last_status

    def set_status_callback(self, callback: Callable[[object], None]) -> None:
        self._status_callback = callback

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback

    def connect(self, verify_idn: Callable[[str], bool], describe: str = "instrument") -> None:
        if self._connected:
            return
        try:
            self._link.open()
        except OSError as e:
            raise ScpiError(f"Cannot reach {describe}: {e}") from e
        try:
            idn = self.query("*IDN?")
        except OSError as e:
            self._link.close()
            raise ScpiError(f"No SCPI response from {describe}: {e}") from e
        if not verify_idn(idn):
            self._link.close()
            raise ScpiError(f"Unexpected instrument at {describe}: {idn!r}")
        self._identity = idn.strip()
        self._connected = True

    def start_polling(self, poll_once: Callable[[], object]) -> None:
        self._poll_fn = poll_once
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=self._poll_interval + self._lock_timeout + 2.0)
        self._poll_thread = None
        self._link.close()
        self._connected = False
        self._last_status = None

    def command(self, cmd: str) -> bool:
        """Fire a set-command with the GUI lock timeout. Never raises on I/O."""
        if not self._lock.acquire(timeout=self._lock_timeout):
            self._report_error(f"Instrument busy, command dropped: {cmd}")
            return False
        try:
            self.write(cmd)
            return True
        except OSError as e:
            self._report_error(f"Instrument command failed: {e}")
            return False
        finally:
            self._lock.release()

    def run_locked(self, fn: Callable[[], object]) -> object:
        with self._lock:
            return fn()

    def write(self, cmd: str) -> None:
        self._link.send((cmd + "\n").encode("ascii"))

    def read_line(self) -> str:
        chunks = []
        while True:
            data = self._link.recv(4096)
            if not data:
                raise OSError("Connection closed by instrument")
            chunks.append(data)
            if data.endswith(b"\n"):
                break
        return b"".join(chunks).decode("ascii").strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read_line()

    def _poll_tick(self) -> None:
        try:
            status = self._poll_fn()
            self._last_status = status
            if self._status_callback:
                try:
                    self._status_callback(status)
                except Exception:
                    pass
        except Exception as e:
            self._last_status = None
            if self._running:
                self._report_error(f"Instrument poll failed: {e}")

    def _poll_loop(self) -> None:
        while self._running:
            start = time.monotonic()
            self._poll_tick()
            remaining = self._poll_interval - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)

    def _report_error(self, message: str) -> None:
        if self._error_callback:
            try:
                self._error_callback(message)
            except Exception:
                pass
```

- [ ] **Step 4: Rewrite `protocol/rigol_dp832a.py` onto the transport**

Replace the whole file with:

```python
"""Rigol DP832A power supply driver - SCPI over LAN via ScpiTransport.

The generic plumbing (socket, framing, lock-timeout commands, poll thread,
stale-status invalidation) lives in scpi_transport.py; this class supplies
only DP832A-specific SCPI strings and status parsing. Status callbacks fire
on the poll thread - GUI consumers must marshal through a Qt Signal.
"""

from typing import Callable, Optional

from .dp832a_protocol import ChargerStatus, DP832AProtocol
from .scpi_transport import LanScpiLink, ScpiError, ScpiTransport


class ChargerError(ScpiError):
    """Raised on DP832A connection or identification failures."""


class RigolDP832A:
    DEFAULT_PORT = 5555
    POLL_INTERVAL = 1.0  # seconds
    SOCKET_TIMEOUT = 2.0  # seconds
    GUI_LOCK_TIMEOUT = 1.0  # seconds

    def __init__(self, transport: Optional[ScpiTransport] = None) -> None:
        # transport injection is a test seam; connect() builds a LAN one.
        self._transport = transport
        self._channel = 1
        self._host: Optional[str] = None
        self._status_callback: Optional[Callable[[ChargerStatus], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None
        if transport is not None:
            self._apply_callbacks()

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected if self._transport else False

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def identity(self) -> str:
        return self._transport.identity if self._transport else ""

    @property
    def channel(self) -> int:
        return self._channel

    @property
    def last_status(self) -> Optional[ChargerStatus]:
        return self._transport.last_status if self._transport else None

    def set_channel(self, channel: int) -> None:
        DP832AProtocol.check_channel(channel)
        self._channel = channel

    def set_status_callback(self, callback: Callable[[ChargerStatus], None]) -> None:
        self._status_callback = callback
        self._apply_callbacks()

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback
        self._apply_callbacks()

    def connect(self, host: str, port: int = DEFAULT_PORT) -> bool:
        if self.is_connected:
            return True
        transport = self._transport or ScpiTransport(
            LanScpiLink(host, port, timeout=self.SOCKET_TIMEOUT),
            poll_interval=self.POLL_INTERVAL,
            lock_timeout=self.GUI_LOCK_TIMEOUT,
        )
        try:
            transport.connect(
                DP832AProtocol.parse_idn, describe=f"DP832A at {host}:{port}"
            )
        except ScpiError as e:
            raise ChargerError(str(e)) from e
        self._transport = transport
        self._apply_callbacks()
        self._host = host
        transport.start_polling(self._poll_once)
        return True

    def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.disconnect()

    def set_voltage(self, volts: float) -> bool:
        return self._command(DP832AProtocol.cmd_set_voltage(self._channel, volts))

    def set_current(self, amps: float) -> bool:
        return self._command(DP832AProtocol.cmd_set_current(self._channel, amps))

    def set_ovp(self, volts: float) -> bool:
        ok = self._command(DP832AProtocol.cmd_set_ovp_value(self._channel, volts))
        return ok and self._command(DP832AProtocol.cmd_set_ovp_state(self._channel, True))

    def output_on(self) -> bool:
        return self._command(DP832AProtocol.cmd_set_output(self._channel, True))

    def output_off(self) -> bool:
        return self._command(DP832AProtocol.cmd_set_output(self._channel, False))

    def _command(self, cmd: str) -> bool:
        if self._transport is None:
            return False
        return self._transport.command(cmd)

    def _apply_callbacks(self) -> None:
        if self._transport is None:
            return
        if self._status_callback is not None:
            self._transport.set_status_callback(self._status_callback)
        if self._error_callback is not None:
            self._transport.set_error_callback(self._error_callback)

    def _poll_once(self) -> ChargerStatus:
        """Read one status snapshot; runs under the transport lock."""
        proto = DP832AProtocol
        transport = self._transport

        def read() -> ChargerStatus:
            ch = self._channel
            volts, amps, watts = proto.parse_measure_all(
                transport.query(proto.cmd_measure_all(ch))
            )
            output_on = proto.parse_output_state(
                transport.query(proto.cmd_query_output(ch))
            )
            mode = (
                proto.parse_mode(transport.query(proto.cmd_query_mode(ch)))
                if output_on
                else "UR"
            )
            return ChargerStatus(
                voltage_v=volts,
                current_a=amps,
                power_w=watts,
                output_on=output_on,
                mode=mode,
                channel=ch,
            )

        return transport.run_locked(read)
```

- [ ] **Step 5: Update `tests/test_rigol_dp832a.py` to the link seam**

Replace the whole file with (same behavioral coverage; the two `_poll_tick` invalidation tests now live in `test_scpi_transport.py`):

```python
"""Tests for the RigolDP832A driver over a scripted fake SCPI link."""

from load_test_bench.protocol.rigol_dp832a import RigolDP832A
from load_test_bench.protocol.scpi_transport import ScpiTransport


class FakeLink:
    """Scripted ScpiLink speaking DP832A SCPI (line-framed)."""

    def __init__(self, responses=None):
        self.responses = {
            "*IDN?": "RIGOL TECHNOLOGIES,DP832A,DP8A123456789,00.01.16\n",
            ":MEAS:ALL? CH1": "4.105,0.512,2.102\n",
            ":OUTP? CH1": "ON\n",
            ":OUTP:MODE? CH1": "CC\n",
        }
        if responses:
            self.responses.update(responses)
        self.sent = []
        self._pending = b""

    def open(self):
        pass

    def close(self):
        pass

    def send(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, max_bytes):
        data, self._pending = self._pending, b""
        return data


class BrokenLink(FakeLink):
    """Link whose writes always fail."""

    def send(self, data):
        raise OSError("network unreachable")


def make_device(link=None):
    """Device wired to a fake link, bypassing connect() (no poll thread)."""
    transport = ScpiTransport(link if link is not None else FakeLink())
    transport._connected = True
    return RigolDP832A(transport=transport)


def sent(device):
    return device._transport._link.sent


class TestCommands:
    def test_set_voltage_sends_scpi(self):
        device = make_device()
        assert device.set_voltage(4.2) is True
        assert sent(device) == [":SOUR1:VOLT 4.200"]

    def test_channel_selection_changes_commands(self):
        """Commands target whichever channel was selected."""
        device = make_device()
        device.set_channel(2)
        device.set_current(1.5)
        assert sent(device) == [":SOUR2:CURR 1.500"]

    def test_output_on_off(self):
        device = make_device()
        device.output_on()
        device.output_off()
        assert sent(device) == [":OUTP CH1,ON", ":OUTP CH1,OFF"]

    def test_set_ovp_sends_value_then_enable(self):
        device = make_device()
        assert device.set_ovp(4.3) is True
        assert sent(device) == [":OUTP:OVP:VAL CH1,4.300", ":OUTP:OVP CH1,ON"]

    def test_command_failure_returns_false_and_reports(self):
        """I/O errors surface via the error callback, never as exceptions."""
        device = make_device(BrokenLink())
        errors = []
        device.set_error_callback(errors.append)
        assert device.set_voltage(4.2) is False
        assert len(errors) == 1

    def test_commands_without_transport_return_false(self):
        """A never-connected device drops commands instead of raising."""
        device = RigolDP832A()
        assert device.output_off() is False


class TestPolling:
    def test_poll_once_builds_status(self):
        """One poll pass reads V/I/P, output state, and regulation mode."""
        device = make_device()
        status = device._poll_once()
        assert status.voltage_v == 4.105
        assert status.current_a == 0.512
        assert status.power_w == 2.102
        assert status.output_on is True
        assert status.mode == "CC"
        assert status.channel == 1

    def test_poll_once_output_off_reports_ur_without_mode_query(self):
        """With the output off the mode query is skipped; mode reads UR."""
        device = make_device(FakeLink({":OUTP? CH1": "OFF\n"}))
        status = device._poll_once()
        assert status.output_on is False
        assert status.mode == "UR"
        assert ":OUTP:MODE? CH1" not in sent(device)

    def test_callbacks_set_before_connect_reach_transport(self):
        """Panels set callbacks at construction, before any transport exists."""
        device = RigolDP832A()
        seen = []
        device.set_status_callback(seen.append)
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        device._transport = transport
        device._apply_callbacks()
        transport._running = True
        transport._poll_fn = device._poll_once
        transport._poll_tick()
        assert len(seen) == 1
```

- [ ] **Step 6: Run both test files, then the full suite**

Run: `uv run --extra dev pytest tests/test_scpi_transport.py tests/test_rigol_dp832a.py -v`
Expected: all PASS (10 transport + 9 driver tests)

Run: `uv run --extra dev pytest`
Expected: everything green — the DP832A GUI panel (`dp832a_charger_panel.py`) must keep importing and working unchanged (it only uses the public surface).

- [ ] **Step 7: Commit**

```bash
git add load_test_bench/protocol/scpi_transport.py load_test_bench/protocol/rigol_dp832a.py tests/test_scpi_transport.py tests/test_rigol_dp832a.py
git commit -m "Extract link-agnostic ScpiTransport and refactor DP832A driver onto it"
```

---

### Task 8: Pure phase decision cores (`jobs/cores.py`)

**Files:**
- Create: `load_test_bench/jobs/cores.py`
- Test: `tests/test_phase_cores.py`

**Interfaces:**
- Consumes: `DeviceStatus` duck-typed (`voltage_v`, `load_on` attributes) — tests synthesize statuses.
- Produces (Task 9 shells consume):
  - `DischargeOutcome` Enum: `CONTINUE`, `VOLTAGE_CUTOFF`, `DEVICE_STOPPED`, `TIMEOUT`; `DischargeCore(voltage_cutoff: float, max_duration_s: Optional[float] = None)` with `START_GRACE_S = 3.0`, `start(now_s)`, `update(status, now_s, voltage_override: Optional[float] = None) -> DischargeOutcome`
  - `RestOutcome` Enum: `CONTINUE`, `DONE`; `RestCore(duration_s: float)` with `start(now_s)`, `update(now_s) -> RestOutcome`
  - `TimedOutcome` Enum: `CONTINUE`, `DONE`, `VOLTAGE_CUTOFF`; `TimedCore(duration_s: float, voltage_cutoff: Optional[float] = None)` with `start(now_s)`, `update(status, now_s, voltage_override=None) -> TimedOutcome`
  - `SteppedAction` Enum: `CONTINUE`, `SET_VALUE`, `REST_OFF`, `VOLTAGE_CUTOFF`, `DONE`; `SteppedUpdate(action, value: Optional[float] = None, step_index: int = 0)`; `SteppedCore(steps: list[tuple[float, float]], voltage_cutoff=None, rest_between_steps_s: float = 0.0)` with `start(now_s) -> float` (first value), `update(status, now_s, voltage_override=None) -> SteppedUpdate`, properties `total_steps`/`current_step`
  - `build_sweep_steps(start_value, end_value, divisions, dwell_s) -> list[tuple[float, float]]` — divisions+1 evenly spaced steps (panel semantics)
  - `voltage_override` is the meter-role seam: when a phase runs with `voltage_source: "meter"`, the shell passes the meter voltage here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_phase_cores.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_phase_cores.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.cores'`

- [ ] **Step 3: Implement `jobs/cores.py`**

```python
"""Pure decision cores for job phases.

House pattern (ChargeMonitor): no I/O, no Qt, no clocks - callers supply
now_s (time.monotonic()) and status snapshots; cores return decisions.
voltage_override is the meter-role seam: when a phase is configured with
voltage_source="meter", the shell passes the independent meter voltage and
it takes precedence over the load's own readout.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DischargeOutcome(Enum):
    CONTINUE = "continue"
    VOLTAGE_CUTOFF = "voltage_cutoff"
    DEVICE_STOPPED = "device_stopped"
    TIMEOUT = "timeout"


class DischargeCore:
    """Discharge termination: voltage cutoff, device-side stop, max duration.

    DEVICE_STOPPED only counts after START_GRACE_S - right after turn_on the
    first poll can still report the load off.
    """

    START_GRACE_S = 3.0

    def __init__(self, voltage_cutoff: float, max_duration_s: Optional[float] = None) -> None:
        self.voltage_cutoff = voltage_cutoff
        self.max_duration_s = max_duration_s
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> DischargeOutcome:
        if self.max_duration_s is not None and now_s - self._started_at >= self.max_duration_s:
            return DischargeOutcome.TIMEOUT
        if status is None:
            return DischargeOutcome.CONTINUE
        voltage = voltage_override if voltage_override is not None else status.voltage_v
        if voltage <= self.voltage_cutoff:
            return DischargeOutcome.VOLTAGE_CUTOFF
        if not status.load_on and now_s - self._started_at >= self.START_GRACE_S:
            return DischargeOutcome.DEVICE_STOPPED
        return DischargeOutcome.CONTINUE


class RestOutcome(Enum):
    CONTINUE = "continue"
    DONE = "done"


class RestCore:
    """Trivial duration wait with everything off."""

    def __init__(self, duration_s: float) -> None:
        self.duration_s = duration_s
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, now_s: float) -> RestOutcome:
        if now_s - self._started_at >= self.duration_s:
            return RestOutcome.DONE
        return RestOutcome.CONTINUE


class TimedOutcome(Enum):
    CONTINUE = "continue"
    DONE = "done"
    VOLTAGE_CUTOFF = "voltage_cutoff"


class TimedCore:
    """Fixed-duration run with an optional safety voltage cutoff."""

    def __init__(self, duration_s: float, voltage_cutoff: Optional[float] = None) -> None:
        self.duration_s = duration_s
        self.voltage_cutoff = voltage_cutoff
        self._started_at = 0.0

    def start(self, now_s: float) -> None:
        self._started_at = now_s

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> TimedOutcome:
        if self.voltage_cutoff is not None and status is not None:
            voltage = voltage_override if voltage_override is not None else status.voltage_v
            if voltage <= self.voltage_cutoff:
                return TimedOutcome.VOLTAGE_CUTOFF
        if now_s - self._started_at >= self.duration_s:
            return TimedOutcome.DONE
        return TimedOutcome.CONTINUE


class SteppedAction(Enum):
    CONTINUE = "continue"
    SET_VALUE = "set_value"
    REST_OFF = "rest_off"
    VOLTAGE_CUTOFF = "voltage_cutoff"
    DONE = "done"


@dataclass
class SteppedUpdate:
    action: SteppedAction
    value: Optional[float] = None
    step_index: int = 0


class SteppedCore:
    """Stepped sweep over an explicit (value, dwell_s) list.

    The core returns commands (SET_VALUE / REST_OFF / DONE); the shell
    executes them - which is what makes the sweep logic unit-testable.
    Uniform sweeps come from build_sweep_steps (divisions+1 semantics,
    matching the battery-load/charger panels).
    """

    def __init__(self, steps, voltage_cutoff: Optional[float] = None,
                 rest_between_steps_s: float = 0.0) -> None:
        if not steps:
            raise ValueError("steps must not be empty")
        self.steps = [(float(value), float(dwell)) for value, dwell in steps]
        self.voltage_cutoff = voltage_cutoff
        self.rest_between_steps_s = rest_between_steps_s
        self._index = 0
        self._in_rest = False
        self._mark = 0.0  # start time of the current dwell or rest

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def current_step(self) -> int:
        return self._index

    def start(self, now_s: float) -> float:
        self._index = 0
        self._in_rest = False
        self._mark = now_s
        return self.steps[0][0]

    def update(self, status, now_s: float, voltage_override: Optional[float] = None) -> SteppedUpdate:
        if self.voltage_cutoff is not None and status is not None:
            voltage = voltage_override if voltage_override is not None else status.voltage_v
            if voltage <= self.voltage_cutoff:
                return SteppedUpdate(SteppedAction.VOLTAGE_CUTOFF, step_index=self._index)
        if self._in_rest:
            if now_s - self._mark >= self.rest_between_steps_s:
                self._in_rest = False
                self._mark = now_s
                return SteppedUpdate(
                    SteppedAction.SET_VALUE,
                    value=self.steps[self._index][0],
                    step_index=self._index,
                )
            return SteppedUpdate(SteppedAction.CONTINUE, step_index=self._index)
        _, dwell = self.steps[self._index]
        if now_s - self._mark >= dwell:
            self._index += 1
            if self._index >= len(self.steps):
                return SteppedUpdate(SteppedAction.DONE, step_index=len(self.steps))
            if self.rest_between_steps_s > 0:
                self._in_rest = True
                self._mark = now_s
                return SteppedUpdate(SteppedAction.REST_OFF, step_index=self._index)
            self._mark = now_s
            return SteppedUpdate(
                SteppedAction.SET_VALUE,
                value=self.steps[self._index][0],
                step_index=self._index,
            )
        return SteppedUpdate(SteppedAction.CONTINUE, step_index=self._index)


def build_sweep_steps(start_value: float, end_value: float, divisions: int, dwell_s: float):
    """divisions+1 evenly spaced steps from start to end (panel semantics)."""
    if divisions < 1:
        return [(float(start_value), float(dwell_s))]
    step_size = (end_value - start_value) / divisions
    return [
        (start_value + index * step_size, float(dwell_s))
        for index in range(divisions + 1)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_phase_cores.py -v`
Expected: all PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/cores.py tests/test_phase_cores.py
git commit -m "Add pure decision cores for discharge, rest, timed, and stepped phases"
```

---

### Task 9: Phase shells and registry (`jobs/phases.py`)

**Files:**
- Create: `load_test_bench/jobs/phases.py`
- Test: `tests/test_phases.py`

**Interfaces:**
- Consumes: Task 8 cores; Task 2 `PhaseSpec`/`PhaseResult`/`PhaseState`; Task 4 fakes.
- Produces (Task 11 consumes):
  - `PhaseReporter` Protocol: `on_progress(progress: dict) -> None`
  - `PhaseTick(done: bool = False, result: Optional[PhaseResult] = None)`; module constant `CONTINUE = PhaseTick()` (never mutate)
  - `PhaseContext(load=None, psu=None, meter=None, report=None, settle=<no-op>)`
  - `Phase` ABC: class attrs `creates_session = True`, `uses_load = True`; methods `on_enter(ctx, now_s) -> bool` (idempotent — safe to retry), `tick(ctx, now_s) -> PhaseTick` (non-blocking), `on_pause(ctx) -> None`, `on_resume(ctx, now_s) -> bool`, `on_exit(ctx, reason: str) -> None`
  - `DischargePhase` (params: `current_a`, `voltage_cutoff`, optional `max_duration_s`, optional `voltage_source`), `RestPhase` (params: `duration_s`; `creates_session = False`), `TimedPhase` (params: `current_a`, `duration_s`, optional `voltage_cutoff`), `SteppedPhase` (params: `steps` as `[[value, dwell_s], ...]` OR `start_value`/`end_value`/`divisions`/`dwell_s`, optional `mode` "current"|"resistance", `voltage_cutoff`, `rest_between_steps_s`)
  - `PHASE_TYPES: dict[str, type[Phase]]` and `build_phase(spec: PhaseSpec) -> Phase` (raises `ValueError` on unknown type or bad params) — the domain-neutrality extension point: new domains register a Phase class here, the engine never changes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_phases.py`:

```python
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
        phase.on_pause(ctx_with(load))
        assert load.on is False
        assert phase.on_resume(ctx_with(load), now_s=10.0) is True
        assert load.on is True

    def test_on_exit_turns_load_off(self):
        phase, load = self.make(), FakeLoad()
        phase.on_enter(ctx_with(load), now_s=0.0)
        phase.on_exit(ctx_with(load), reason="voltage_cutoff")
        assert load.on is False


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


class TestBuildPhase:
    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError):
            build_phase(PhaseSpec("espresso", {}))

    def test_missing_params_rejected(self):
        with pytest.raises(ValueError):
            build_phase(PhaseSpec("discharge", {}))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_phases.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.phases'`

- [ ] **Step 3: Implement `jobs/phases.py`**

```python
"""Phase actuation shells: thin device I/O around the pure cores.

Prefect seam contract (see jobs/__init__.py): a phase takes JSON-serializable
PhaseSpec.params plus an injected PhaseContext, reports only through
PhaseReporter, returns a JSON-serializable PhaseResult, keeps tick()
non-blocking, and has idempotent edges - on_enter establishes full device
state from params; on_exit makes its device safe.

Domain neutrality: the engine dispatches purely through PHASE_TYPES. A new
domain (PSU burn-in, converter characterization, ...) adds a Phase subclass
and registers it here - engine, ledger, and recovery are untouched.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .cores import (
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
from .model import PhaseResult, PhaseSpec, PhaseState


class PhaseReporter(Protocol):
    def on_progress(self, progress: dict) -> None: ...


@dataclass
class PhaseTick:
    done: bool = False
    result: Optional[PhaseResult] = None


CONTINUE = PhaseTick()  # shared sentinel - never mutate


def _no_settle(seconds: float) -> None:
    return None


@dataclass
class PhaseContext:
    """Everything a phase may touch - injected, nothing global, nothing Qt."""

    load: Optional[object] = None
    psu: Optional[object] = None
    meter: Optional[object] = None
    report: Optional[PhaseReporter] = None
    settle: Callable[[float], None] = field(default=_no_settle)


class Phase(ABC):
    creates_session = True  # phases that produce readings get a sessions row
    uses_load = True

    def __init__(self, spec: PhaseSpec) -> None:
        self.spec = spec

    @abstractmethod
    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        """Establish full device state from params. Idempotent; True on success."""

    @abstractmethod
    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        """One non-blocking decision step."""

    def on_pause(self, ctx: PhaseContext) -> None:
        if ctx.load is not None:
            ctx.load.turn_off()

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        return True

    def on_exit(self, ctx: PhaseContext, reason: str) -> None:
        if ctx.load is not None:
            ctx.load.turn_off()

    def _meter_voltage(self, ctx: PhaseContext) -> Optional[float]:
        """The meter-role seam: independent voltage when configured."""
        if self.spec.params.get("voltage_source") != "meter" or ctx.meter is None:
            return None
        status = ctx.meter.last_status
        return status.voltage_v if status is not None else None

    @staticmethod
    def _metrics(status) -> dict:
        if status is None:
            return {}
        return {
            "capacity_mah": getattr(status, "capacity_mah", 0.0),
            "energy_wh": getattr(status, "energy_wh", 0.0),
        }


class DischargePhase(Phase):
    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        self._current_a = float(params["current_a"])
        self._voltage_cutoff = float(params["voltage_cutoff"])
        max_duration = params.get("max_duration_s")
        self._core = DischargeCore(
            self._voltage_cutoff,
            float(max_duration) if max_duration is not None else None,
        )

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        ctx.settle(0.5)
        ok = load.set_current(self._current_a) and ok
        ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        ok = load.turn_on() and ok
        if ok:
            self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        outcome = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if outcome == DischargeOutcome.CONTINUE:
            return CONTINUE
        return PhaseTick(
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED, reason=outcome.value, metrics=self._metrics(status)
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None:
            return False
        ok = load.set_current(self._current_a)
        return load.turn_on() and ok


class RestPhase(Phase):
    creates_session = False
    uses_load = False

    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        self._core = RestCore(float(spec.params["duration_s"]))

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        ok = True
        if ctx.load is not None and ctx.load.is_connected:
            ok = ctx.load.turn_off()
        if ctx.psu is not None and ctx.psu.is_connected:
            ok = ctx.psu.output_off() and ok
        self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        if self._core.update(now_s) == RestOutcome.DONE:
            return PhaseTick(
                done=True, result=PhaseResult(PhaseState.COMPLETED, reason="rest_complete")
            )
        return CONTINUE

    def on_pause(self, ctx: PhaseContext) -> None:
        return None  # nothing running


class TimedPhase(Phase):
    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        self._current_a = float(params["current_a"])
        cutoff = params.get("voltage_cutoff")
        self._voltage_cutoff = float(cutoff) if cutoff is not None else None
        self._core = TimedCore(float(params["duration_s"]), self._voltage_cutoff)

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        ctx.settle(0.5)
        ok = load.set_current(self._current_a) and ok
        if self._voltage_cutoff is not None:
            ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        ok = load.turn_on() and ok
        if ok:
            self._core.start(now_s)
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        outcome = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if outcome == TimedOutcome.CONTINUE:
            return CONTINUE
        reason = "duration_complete" if outcome == TimedOutcome.DONE else outcome.value
        return PhaseTick(
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED, reason=reason, metrics=self._metrics(status)
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None:
            return False
        ok = load.set_current(self._current_a)
        return load.turn_on() and ok


class SteppedPhase(Phase):
    COMMAND_FAILURE_LIMIT = 3

    def __init__(self, spec: PhaseSpec) -> None:
        super().__init__(spec)
        params = spec.params
        if "steps" in params:
            steps = [
                (step[0], step[1])
                if isinstance(step, (list, tuple))
                else (step["value"], step["dwell_s"])
                for step in params["steps"]
            ]
        else:
            steps = build_sweep_steps(
                params["start_value"],
                params["end_value"],
                int(params.get("divisions", 1)),
                params["dwell_s"],
            )
        self._mode = params.get("mode", "current")
        if self._mode not in ("current", "resistance"):
            raise ValueError(f"Unknown stepped mode: {self._mode}")
        cutoff = params.get("voltage_cutoff")
        self._voltage_cutoff = float(cutoff) if cutoff is not None else None
        self._core = SteppedCore(
            steps,
            voltage_cutoff=self._voltage_cutoff,
            rest_between_steps_s=float(params.get("rest_between_steps_s", 0.0)),
        )
        self._command_failures = 0
        self._after_rest = False

    def _apply_value(self, ctx: PhaseContext, value: float) -> bool:
        if self._mode == "resistance":
            return ctx.load.set_resistance(value)
        return ctx.load.set_current(value)

    def _note_command(self, ok: bool) -> None:
        self._command_failures = 0 if ok else self._command_failures + 1

    def _fault(self) -> PhaseTick:
        return PhaseTick(
            done=True,
            result=PhaseResult(PhaseState.FAULTED, reason="device_command_failed"),
        )

    def on_enter(self, ctx: PhaseContext, now_s: float) -> bool:
        load = ctx.load
        if load is None or not load.is_connected:
            return False
        ok = load.reset_counters()
        if self._voltage_cutoff is not None:
            ok = load.set_voltage_cutoff(self._voltage_cutoff) and ok
        ctx.settle(0.5)
        first_value = self._core.start(now_s)
        ok = self._apply_value(ctx, first_value) and ok
        ok = load.turn_on() and ok
        self._command_failures = 0
        self._after_rest = False
        return ok

    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick:
        status = ctx.load.last_status if ctx.load is not None else None
        update = self._core.update(
            status, now_s, voltage_override=self._meter_voltage(ctx)
        )
        if update.action == SteppedAction.CONTINUE:
            return CONTINUE
        if update.action == SteppedAction.SET_VALUE:
            ok = self._apply_value(ctx, update.value)
            if self._after_rest:
                ok = ctx.load.turn_on() and ok
                self._after_rest = False
            self._note_command(ok)
            if self._command_failures >= self.COMMAND_FAILURE_LIMIT:
                return self._fault()
            if ctx.report is not None:
                ctx.report.on_progress(
                    {"step": update.step_index, "total_steps": self._core.total_steps}
                )
            return CONTINUE
        if update.action == SteppedAction.REST_OFF:
            self._after_rest = True
            self._note_command(ctx.load.turn_off())
            if self._command_failures >= self.COMMAND_FAILURE_LIMIT:
                return self._fault()
            return CONTINUE
        if update.action == SteppedAction.VOLTAGE_CUTOFF:
            return PhaseTick(
                done=True,
                result=PhaseResult(
                    PhaseState.COMPLETED,
                    reason="voltage_cutoff",
                    metrics=self._metrics(status),
                ),
            )
        return PhaseTick(  # DONE
            done=True,
            result=PhaseResult(
                PhaseState.COMPLETED,
                reason="sweep_complete",
                metrics=self._metrics(status),
            ),
        )

    def on_resume(self, ctx: PhaseContext, now_s: float) -> bool:
        index = min(self._core.current_step, self._core.total_steps - 1)
        ok = self._apply_value(ctx, self._core.steps[index][0])
        return ctx.load.turn_on() and ok


PHASE_TYPES = {
    "discharge": DischargePhase,
    "rest": RestPhase,
    "timed": TimedPhase,
    "stepped": SteppedPhase,
}


def build_phase(spec: PhaseSpec) -> Phase:
    cls = PHASE_TYPES.get(spec.phase_type)
    if cls is None:
        raise ValueError(f"Unknown phase type: {spec.phase_type}")
    try:
        return cls(spec)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid params for phase '{spec.phase_type}': {e}") from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_phases.py -v`
Expected: all PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/phases.py tests/test_phases.py
git commit -m "Add phase actuation shells with type registry"
```

---

### Task 10: Safety rules and supervisor (`jobs/safety.py`)

**Files:**
- Create: `load_test_bench/jobs/safety.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Consumes: `DeviceStatus` duck-typed (`mosfet_temp_c`, `ext_temp_c`, `load_on`), `ChargerStatus` duck-typed (`current_a`, `output_on`).
- Produces (Tasks 11/13 consume):
  - `SafetyConfig(mosfet_temp_max_c=80.0, ext_temp_max_c=60.0, psu_current_max_a=None, stale_status_timeout_s=10.0, temp_hysteresis_c=5.0)` — `ext_temp_max_c=None` disables the external-probe rule; `psu_current_max_a=None` means disabled until the operator configures it
  - `Trip(rule: str, message: str)` (frozen)
  - `SafetyRules(config)`: `evaluate_load(status) -> list[Trip]`, `evaluate_psu(status) -> list[Trip]`, `is_clear_load(status) -> bool`, `is_clear_psu(status) -> bool` (clear = below threshold minus hysteresis)
  - `SafetySupervisor(rules, on_trip: Optional[Callable[[str], None]] = None)`: `observe_load(status, now_s)`, `observe_psu(status, now_s)` (called from poll threads — pure eval + latch only), `check_stale(now_s)` (called from the engine thread), properties `tripped: bool` / `trip_reason: str`, `try_reset() -> bool` (only when currently clear). `on_trip(reason)` fires exactly once per latch, from whichever thread observed the trip — it must only set events/emit signals.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_safety.py`:

```python
"""Tests for the actuating safety layer (rules + latching supervisor)."""

from dataclasses import dataclass

from load_test_bench.jobs.safety import (
    SafetyConfig,
    SafetyRules,
    SafetySupervisor,
)


@dataclass
class LoadStatus:
    mosfet_temp_c: int = 40
    ext_temp_c: int = 25
    load_on: bool = True


@dataclass
class PsuStatus:
    current_a: float = 1.0
    output_on: bool = True


class TestSafetyRules:
    def test_mosfet_over_temp_trips(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0))
        trips = rules.evaluate_load(LoadStatus(mosfet_temp_c=80))
        assert len(trips) == 1
        assert trips[0].rule == "mosfet_over_temp"

    def test_below_threshold_is_quiet(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0))
        assert rules.evaluate_load(LoadStatus(mosfet_temp_c=79)) == []

    def test_ext_probe_rule_disabled_when_none(self):
        rules = SafetyRules(SafetyConfig(ext_temp_max_c=None))
        assert rules.evaluate_load(LoadStatus(ext_temp_c=200)) == []

    def test_ext_probe_zero_reading_means_absent(self):
        """A 0 reading means no probe attached - never a trip."""
        rules = SafetyRules(SafetyConfig(ext_temp_max_c=60.0))
        assert rules.evaluate_load(LoadStatus(ext_temp_c=0)) == []
        assert len(rules.evaluate_load(LoadStatus(ext_temp_c=60))) == 1

    def test_psu_ceiling_disabled_by_default(self):
        rules = SafetyRules(SafetyConfig())
        assert rules.evaluate_psu(PsuStatus(current_a=99.0)) == []

    def test_psu_ceiling_trips_when_configured(self):
        rules = SafetyRules(SafetyConfig(psu_current_max_a=2.0))
        trips = rules.evaluate_psu(PsuStatus(current_a=2.5))
        assert len(trips) == 1
        assert trips[0].rule == "psu_over_current"

    def test_clear_requires_hysteresis_margin(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0, temp_hysteresis_c=5.0))
        assert rules.is_clear_load(LoadStatus(mosfet_temp_c=78)) is False  # < 80 but not < 75
        assert rules.is_clear_load(LoadStatus(mosfet_temp_c=74)) is True


class TestSafetySupervisor:
    def make(self, **config):
        reasons = []
        supervisor = SafetySupervisor(
            SafetyRules(SafetyConfig(**config)), on_trip=reasons.append
        )
        return supervisor, reasons

    def test_trip_latches_and_fires_once(self):
        supervisor, reasons = self.make(mosfet_temp_max_c=80.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=85), now_s=1.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=86), now_s=2.0)
        assert supervisor.tripped is True
        assert "mosfet" in supervisor.trip_reason
        assert len(reasons) == 1

    def test_stale_status_trips_when_output_believed_on(self):
        supervisor, reasons = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=0.0)
        supervisor.check_stale(now_s=9.0)
        assert supervisor.tripped is False
        supervisor.check_stale(now_s=10.0)
        assert supervisor.tripped is True
        assert "stale" in supervisor.trip_reason

    def test_no_stale_trip_when_output_off(self):
        supervisor, _ = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=False), now_s=0.0)
        supervisor.check_stale(now_s=100.0)
        assert supervisor.tripped is False

    def test_fresh_status_resets_staleness(self):
        supervisor, _ = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=0.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=8.0)
        supervisor.check_stale(now_s=15.0)
        assert supervisor.tripped is False

    def test_reset_refused_while_condition_persists(self):
        supervisor, _ = self.make(mosfet_temp_max_c=80.0, temp_hysteresis_c=5.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=85), now_s=1.0)
        assert supervisor.try_reset() is False
        supervisor.observe_load(LoadStatus(mosfet_temp_c=78), now_s=2.0)
        assert supervisor.try_reset() is False  # inside hysteresis band
        supervisor.observe_load(LoadStatus(mosfet_temp_c=70), now_s=3.0)
        assert supervisor.try_reset() is True
        assert supervisor.tripped is False

    def test_reset_when_not_tripped_is_true(self):
        supervisor, _ = self.make()
        assert supervisor.try_reset() is True

    def test_psu_observation_trips(self):
        supervisor, reasons = self.make(psu_current_max_a=2.0)
        supervisor.observe_psu(PsuStatus(current_a=3.0), now_s=1.0)
        assert supervisor.tripped is True
        assert len(reasons) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_safety.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.safety'`

- [ ] **Step 3: Implement `jobs/safety.py`**

```python
"""Actuating safety layer: conservative invariants that cut hardware outputs.

Deliberately separate from alerts/ (notify-only, user-tunable). Rules are
pure; the supervisor latches the first trip and notifies via on_trip. All
actuation happens on the engine thread - observe_* run on device poll
threads and must stay microsecond-cheap.
"""

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class SafetyConfig:
    mosfet_temp_max_c: float = 80.0
    ext_temp_max_c: Optional[float] = 60.0  # None disables the rule
    psu_current_max_a: Optional[float] = None  # disabled until configured
    stale_status_timeout_s: float = 10.0
    temp_hysteresis_c: float = 5.0


@dataclass(frozen=True)
class Trip:
    rule: str
    message: str


class SafetyRules:
    def __init__(self, config: SafetyConfig) -> None:
        self.config = config

    def evaluate_load(self, status) -> list:
        trips = []
        if status.mosfet_temp_c >= self.config.mosfet_temp_max_c:
            trips.append(
                Trip(
                    "mosfet_over_temp",
                    f"load mosfet {status.mosfet_temp_c}°C >= {self.config.mosfet_temp_max_c}°C",
                )
            )
        if (
            self.config.ext_temp_max_c is not None
            and status.ext_temp_c > 0  # 0 = probe absent
            and status.ext_temp_c >= self.config.ext_temp_max_c
        ):
            trips.append(
                Trip(
                    "ext_over_temp",
                    f"external probe {status.ext_temp_c}°C >= {self.config.ext_temp_max_c}°C",
                )
            )
        return trips

    def evaluate_psu(self, status) -> list:
        trips = []
        if (
            self.config.psu_current_max_a is not None
            and status.current_a >= self.config.psu_current_max_a
        ):
            trips.append(
                Trip(
                    "psu_over_current",
                    f"psu current {status.current_a} A >= {self.config.psu_current_max_a} A",
                )
            )
        return trips

    def is_clear_load(self, status) -> bool:
        margin = self.config.temp_hysteresis_c
        if status.mosfet_temp_c >= self.config.mosfet_temp_max_c - margin:
            return False
        if (
            self.config.ext_temp_max_c is not None
            and status.ext_temp_c > 0
            and status.ext_temp_c >= self.config.ext_temp_max_c - margin
        ):
            return False
        return True

    def is_clear_psu(self, status) -> bool:
        if self.config.psu_current_max_a is None:
            return True
        return status.current_a < self.config.psu_current_max_a

    @property
    def stale_status_timeout_s(self) -> float:
        return self.config.stale_status_timeout_s


class SafetySupervisor:
    """Latching trip supervisor fed by both device status pipelines."""

    def __init__(self, rules: SafetyRules, on_trip: Optional[Callable[[str], None]] = None) -> None:
        self._rules = rules
        self._on_trip = on_trip
        self._lock = threading.Lock()
        self._tripped = False
        self._trip_reason = ""
        self._load_status = None
        self._load_seen_s: Optional[float] = None
        self._psu_status = None
        self._psu_seen_s: Optional[float] = None

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def observe_load(self, status, now_s: float) -> None:
        with self._lock:
            self._load_status = status
            self._load_seen_s = now_s
            trips = self._rules.evaluate_load(status)
        self._latch(trips)

    def observe_psu(self, status, now_s: float) -> None:
        with self._lock:
            self._psu_status = status
            self._psu_seen_s = now_s
            trips = self._rules.evaluate_psu(status)
        self._latch(trips)

    def check_stale(self, now_s: float) -> None:
        """Engine-thread watchdog: no fresh status while an output is on."""
        timeout = self._rules.stale_status_timeout_s
        trips = []
        with self._lock:
            if (
                self._load_status is not None
                and getattr(self._load_status, "load_on", False)
                and self._load_seen_s is not None
                and now_s - self._load_seen_s >= timeout
            ):
                trips.append(
                    Trip("load_status_stale", "load status stale while output on")
                )
            if (
                self._psu_status is not None
                and getattr(self._psu_status, "output_on", False)
                and self._psu_seen_s is not None
                and now_s - self._psu_seen_s >= timeout
            ):
                trips.append(Trip("psu_status_stale", "psu status stale while output on"))
        self._latch(trips)

    def try_reset(self) -> bool:
        """Clear the latch - only when every rule currently evaluates clear."""
        with self._lock:
            if not self._tripped:
                return True
            load_clear = self._load_status is None or self._rules.is_clear_load(
                self._load_status
            )
            psu_clear = self._psu_status is None or self._rules.is_clear_psu(
                self._psu_status
            )
            if load_clear and psu_clear:
                self._tripped = False
                self._trip_reason = ""
                return True
            return False

    def _latch(self, trips) -> None:
        if not trips:
            return
        fire = False
        with self._lock:
            if not self._tripped:
                self._tripped = True
                self._trip_reason = "; ".join(trip.message for trip in trips)
                fire = True
        if fire and self._on_trip is not None:
            try:
                self._on_trip(self._trip_reason)
            except Exception:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_safety.py -v`
Expected: all PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add load_test_bench/jobs/safety.py tests/test_safety.py
git commit -m "Add actuating safety rules and latching supervisor"
```

---

### Task 11: JobExecutor + JobEngine (`jobs/engine.py`)

**Files:**
- Create: `load_test_bench/jobs/engine.py`
- Test: `tests/test_job_executor.py`

**Interfaces:**
- Consumes: Task 2 model, Task 3 `JobLedger`, Task 4 `DeviceRegistry` + fakes, Task 5 `make_safe`, Task 9 `build_phase`/`PhaseContext`, Task 10 `SafetySupervisor`, `Database`, `TestSession`/`Reading` from `data/models.py`.
- Produces (Tasks 12/13 consume):
  - `JobExecutor(ledger, registry, database, reading_sink: Optional[Callable[[int, Reading], None]] = None, supervisor=None, settle=None)` — constants `HEARTBEAT_INTERVAL_S = 5.0`, `READING_INTERVAL_S = 1.0`, `ENTER_RETRY_LIMIT = 3`; methods `submit(spec) -> int` (raises `RuntimeError` when safety-tripped), `step(now_s) -> None`, `pause()`, `resume()`, `stop()`, `abort_for_safety(reason)`, `add_snapshot_callback(cb: Callable[[JobSnapshot], None])`; properties `ledger`, `has_active_job -> bool`, `active_job_id -> Optional[int]`. Snapshot callbacks fire on whatever thread calls `step()` (the engine thread in production).
  - `JobEngine(executor)` — `TICK_INTERVAL_S = 1.0`; `start()`, `shutdown(timeout: float = 5.0)`, `wake()`, `submit(spec) -> int`, `pause()`, `resume()`, `stop()`; property `executor`. The engine thread is a thin loop (`step`; `wake.wait(1.0)`) and is untested-by-design.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_job_executor.py`:

```python
"""Tests for the thread-free JobExecutor (fake devices, fake clock, tmp DB)."""

from dataclasses import dataclass
from typing import Optional

import pytest

from load_test_bench.data.database import Database
from load_test_bench.jobs.devices import DeviceRegistry
from load_test_bench.jobs.engine import JobExecutor
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_job_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'load_test_bench.jobs.engine'`

- [ ] **Step 3: Implement `jobs/engine.py`**

```python
"""Job execution: thread-free JobExecutor plus the one-thread JobEngine.

JobExecutor owns every orchestration decision and is driven by step(now_s) -
tests call step() directly with a fake clock, no threads. JobEngine is the
thin daemon-thread wrapper: step, then wait up to TICK_INTERVAL_S on a wake
event so safety trips and operator commands get sub-second reaction.

All device commands issued during jobs happen inside step() - i.e. on the
engine thread in production. Snapshot callbacks also fire there; the GUI
bridge marshals them onto the Qt thread.
"""

import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from ..data.database import Database
from ..data.models import Reading, TestSession
from .devices import DeviceRegistry
from .ledger import JobLedger
from .model import JobSnapshot, JobSpec, JobState, PhaseResult, PhaseState
from .phases import Phase, PhaseContext, build_phase
from .recovery import make_safe


class JobExecutor:
    HEARTBEAT_INTERVAL_S = 5.0
    READING_INTERVAL_S = 1.0
    ENTER_RETRY_LIMIT = 3

    def __init__(
        self,
        ledger: JobLedger,
        registry: DeviceRegistry,
        database: Database,
        reading_sink: Optional[Callable[[int, Reading], None]] = None,
        supervisor=None,
        settle: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._database = database
        self._reading_sink = reading_sink or (lambda session_id, reading: None)
        self._supervisor = supervisor
        self._settle = settle if settle is not None else time.sleep
        self._snapshot_callbacks: List[Callable[[JobSnapshot], None]] = []
        self.last_progress: dict = {}

        self._job_id: Optional[int] = None
        self._spec: Optional[JobSpec] = None
        self._state = JobState.PENDING
        self._phase: Optional[Phase] = None
        self._phase_index = 0
        self._phase_session: Optional[TestSession] = None
        self._enter_attempts = 0
        self._job_started_s = 0.0
        self._last_heartbeat_s = 0.0
        self._last_reading_s = -1.0
        self._pause_requested = False
        self._stop_requested = False
        self._abort_reason: Optional[str] = None
        self._safety_handled = False

    @property
    def ledger(self) -> JobLedger:
        return self._ledger

    @property
    def has_active_job(self) -> bool:
        return self._job_id is not None

    @property
    def active_job_id(self) -> Optional[int]:
        return self._job_id

    def add_snapshot_callback(self, callback: Callable[[JobSnapshot], None]) -> None:
        self._snapshot_callbacks.append(callback)

    # --- operator commands (thread-safe: plain flag writes) ---

    def submit(self, spec: JobSpec) -> int:
        if self._supervisor is not None and self._supervisor.tripped:
            raise RuntimeError(
                f"Safety lockout active: {self._supervisor.trip_reason}"
            )
        return self._ledger.create_job(spec)

    def pause(self) -> None:
        self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def abort_for_safety(self, reason: str) -> None:
        self._abort_reason = f"safety: {reason}"

    # --- the tick ---

    def step(self, now_s: float) -> None:
        if self._supervisor is not None:
            self._supervisor.check_stale(now_s)
            if self._supervisor.tripped:
                if self._job_id is not None and self._abort_reason is None:
                    self._abort_reason = f"safety: {self._supervisor.trip_reason}"
                elif self._job_id is None and not self._safety_handled:
                    self._make_safe()
                    self._safety_handled = True
            else:
                self._safety_handled = False

        if self._job_id is None:
            self._maybe_start_next(now_s)
            return
        if self._abort_reason is not None:
            reason = self._abort_reason
            self._abort_reason = None
            self._finish_job(JobState.FAULTED, reason, now_s)
            return
        if self._stop_requested:
            self._stop_requested = False
            self._finish_job(JobState.STOPPED, "stopped by operator", now_s)
            return
        if self._state == JobState.RUNNING and self._pause_requested:
            if self._phase is not None:
                self._phase.on_pause(self._ctx())
            self._state = JobState.PAUSED
            self._ledger.set_job_state(self._job_id, JobState.PAUSED)
            self._emit(now_s, "paused")
            return
        if self._state == JobState.PAUSED:
            if not self._pause_requested:
                if self._phase is not None and self._phase.on_resume(self._ctx(), now_s):
                    self._state = JobState.RUNNING
                    self._ledger.set_job_state(self._job_id, JobState.RUNNING)
                    self._emit(now_s, "resumed")
                else:
                    self._finish_job(JobState.FAULTED, "resume failed", now_s)
            else:
                self._heartbeat_if_due(now_s)
            return
        # RUNNING
        if self._phase is None:
            self._enter_current_phase(now_s)
            return
        self._heartbeat_if_due(now_s)
        self._capture_reading(now_s)
        tick = self._phase.tick(self._ctx(), now_s)
        if tick.done:
            self._complete_phase(tick.result, now_s)
        else:
            self._emit(now_s, "")

    # --- reporter interface (phases call this) ---

    def on_progress(self, progress: dict) -> None:
        self.last_progress = progress

    # --- internals ---

    def _ctx(self) -> PhaseContext:
        return PhaseContext(
            load=self._registry.load,
            psu=self._registry.psu,
            meter=self._registry.meter,
            report=self,
            settle=self._settle,
        )

    def _maybe_start_next(self, now_s: float) -> None:
        pending = self._ledger.next_pending_job()
        if pending is None:
            return
        job_id, spec = pending
        self._job_id, self._spec = job_id, spec
        self._state = JobState.RUNNING
        self._phase = None
        self._phase_index = 0
        self._phase_session = None
        self._enter_attempts = 0
        self._job_started_s = now_s
        self._last_heartbeat_s = now_s
        self._last_reading_s = -1.0
        self._ledger.mark_job_running(job_id)
        self._enter_current_phase(now_s)

    def _enter_current_phase(self, now_s: float) -> None:
        spec = self._spec.phases[self._phase_index]
        if self._phase is None:
            try:
                self._phase = build_phase(spec)
            except ValueError as e:
                self._finish_job(JobState.FAULTED, f"invalid phase spec: {e}", now_s)
                return
        if not self._phase.on_enter(self._ctx(), now_s):
            self._enter_attempts += 1
            if self._enter_attempts >= self.ENTER_RETRY_LIMIT:
                self._finish_job(
                    JobState.FAULTED,
                    f"phase '{spec.phase_type}' failed to start", now_s,
                )
            return  # retried next step (on_enter is idempotent)
        self._enter_attempts = 0
        session_id = None
        if self._phase.creates_session:
            session = TestSession(
                name=f"{self._spec.name} - {spec.phase_type} {self._phase_index + 1}",
                start_time=datetime.now(),
                battery_name=self._spec.battery_name,
                notes=self._spec.notes,
                test_type=spec.phase_type,
                settings=dict(spec.params),
            )
            session.id = self._database.create_session(session)
            self._database.set_session_status(session.id, "running")
            phase_row = self._ledger.phase_row_id(self._job_id, self._phase_index)
            if phase_row is not None:
                self._database.link_session_to_phase(session.id, phase_row)
            self._phase_session = session
            session_id = session.id
        self._ledger.set_current_phase(self._job_id, self._phase_index)
        self._ledger.set_phase_state(
            self._job_id, self._phase_index, PhaseState.RUNNING, session_id=session_id
        )
        self._emit(now_s, f"{spec.phase_type} started")

    def _complete_phase(self, result: PhaseResult, now_s: float) -> None:
        if result.state == PhaseState.FAULTED:
            self._finish_job(JobState.FAULTED, result.reason, now_s)
            return
        self._phase.on_exit(self._ctx(), result.reason)
        self._finalize_phase_session("completed")
        self._ledger.set_phase_state(
            self._job_id, self._phase_index, result.state, result=result
        )
        if self._phase_index + 1 < len(self._spec.phases):
            self._phase_index += 1
            self._phase = None
            self._enter_attempts = 0
            self._emit(now_s, "phase complete")
        else:
            self._finish_job(JobState.COMPLETED, result.reason, now_s)

    def _finish_job(self, state: JobState, reason: str, now_s: float) -> None:
        if state != JobState.COMPLETED:
            self._make_safe()
            if self._phase is not None:
                try:
                    self._phase.on_exit(self._ctx(), reason)
                except Exception:
                    pass
                phase_state = (
                    PhaseState.FAULTED
                    if state == JobState.FAULTED
                    else PhaseState.INTERRUPTED
                )
                self._ledger.set_phase_state(
                    self._job_id,
                    self._phase_index,
                    phase_state,
                    result=PhaseResult(phase_state, reason=reason),
                )
        session_status = {
            JobState.COMPLETED: "completed",
            JobState.FAULTED: "faulted",
        }.get(state, "interrupted")
        self._finalize_phase_session(session_status)
        fault = reason if state in (JobState.FAULTED, JobState.INTERRUPTED) else None
        self._ledger.set_job_state(self._job_id, state, fault_reason=fault)
        self._state = state
        self._emit(now_s, reason)
        self._job_id = None
        self._spec = None
        self._phase = None
        self._phase_session = None
        self._state = JobState.PENDING

    def _finalize_phase_session(self, status: str) -> None:
        session = self._phase_session
        if session is None:
            return
        session.end_time = datetime.now()
        self._database.update_session(session)
        self._database.set_session_status(session.id, status)
        self._database.commit()
        self._phase_session = None

    def _make_safe(self) -> None:
        make_safe(
            load=self._registry.load, psu=self._registry.psu, sleep=self._settle
        )

    def _heartbeat_if_due(self, now_s: float) -> None:
        if now_s - self._last_heartbeat_s >= self.HEARTBEAT_INTERVAL_S:
            self._ledger.heartbeat(self._job_id)
            self._last_heartbeat_s = now_s

    def _capture_reading(self, now_s: float) -> None:
        if self._phase_session is None or self._phase_session.id is None:
            return
        if now_s - self._last_reading_s < self.READING_INTERVAL_S:
            return
        load = self._registry.load
        status = load.last_status if load is not None else None
        if status is None:
            return
        # aux_voltage_v stays NULL until the meter driver lands (see spec)
        reading = Reading(
            timestamp=datetime.now(),
            voltage_v=status.voltage_v,
            current_a=status.current_a,
            power_w=getattr(status, "power_w", 0.0),
            energy_wh=getattr(status, "energy_wh", 0.0),
            capacity_mah=getattr(status, "capacity_mah", 0.0),
            mosfet_temp_c=getattr(status, "mosfet_temp_c", 0),
            ext_temp_c=getattr(status, "ext_temp_c", 0),
            fan_speed_rpm=getattr(status, "fan_speed_rpm", 0),
            load_r_ohm=getattr(status, "load_r_ohm", None),
            battery_r_ohm=getattr(status, "battery_r_ohm", None),
            runtime_s=getattr(status, "runtime_seconds", 0),
        )
        self._reading_sink(self._phase_session.id, reading)
        self._last_reading_s = now_s

    def _emit(self, now_s: float, message: str) -> None:
        if self._job_id is None or self._spec is None:
            return
        snapshot = JobSnapshot(
            job_id=self._job_id,
            state=self._state,
            spec=self._spec,
            phase_index=self._phase_index,
            phase_state=PhaseState.RUNNING if self._phase else PhaseState.PENDING,
            elapsed_s=now_s - self._job_started_s,
            message=message,
        )
        for callback in self._snapshot_callbacks:
            try:
                callback(snapshot)
            except Exception:
                pass


class JobEngine:
    """One daemon thread driving the executor with an interruptible wait."""

    TICK_INTERVAL_S = 1.0

    def __init__(self, executor: JobExecutor) -> None:
        self._executor = executor
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def executor(self) -> JobExecutor:
        return self._executor

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._running = False
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def submit(self, spec: JobSpec) -> int:
        job_id = self._executor.submit(spec)
        self.wake()
        return job_id

    def pause(self) -> None:
        self._executor.pause()
        self.wake()

    def resume(self) -> None:
        self._executor.resume()
        self.wake()

    def stop(self) -> None:
        self._executor.stop()
        self.wake()

    def _run(self) -> None:
        while self._running:
            try:
                self._executor.step(time.monotonic())
            except Exception:
                # The engine thread must never die; executor faults are
                # handled internally - this catches only genuine bugs.
                pass
            self._wake.wait(self.TICK_INTERVAL_S)
            self._wake.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_job_executor.py -v`
Expected: all PASS (10 tests)

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: everything green.

- [ ] **Step 6: Commit**

```bash
git add load_test_bench/jobs/engine.py tests/test_job_executor.py
git commit -m "Add thread-free JobExecutor and JobEngine thread wrapper"
```

---

### Task 12: TestRunner compatibility facade (`automation/test_runner.py` rewrite)

**Files:**
- Modify: `load_test_bench/automation/test_runner.py` (full rewrite below)
- Test: `tests/test_test_runner_facade.py`

**Interfaces:**
- Consumes: Task 11 `JobEngine`/`JobExecutor`, Task 2 model, `automation/profiles.py` (`DischargeProfile`, `CycleProfile`, `TimedProfile`, `SteppedProfile`).
- Produces — the preserved public surface (verified consumers: `main_window.py` calls `start/stop/pause/resume`, `set_progress_callback`, `set_complete_callback`; panels read only `.device` and `.device.is_connected`):
  - `TestState` and `TestProgress` — **verbatim as today** (see code below)
  - `TestRunner(device, database, engine: JobEngine)` — note the new third argument (main_window updated in Task 13); attribute `device` is writable (facade is now created once at startup, device assigned on connect)
  - `start(profile, battery_name="", notes="") -> bool`, `stop()`, `pause()`, `resume()`, properties `state`/`progress`/`is_running`, `set_progress_callback(cb)`, `set_complete_callback(cb)`
  - module function `profile_to_spec(profile, battery_name, notes) -> JobSpec` (raises `ValueError` on unknown type)
  - Callbacks fire on the engine thread — same threading semantics as the old worker-thread TestRunner, so `main_window`'s existing signal-emitting handlers work unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_test_runner_facade.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/test_test_runner_facade.py -v`
Expected: FAIL — `profile_to_spec` doesn't exist and `TestRunner.__init__` takes 2 args.

- [ ] **Step 3: Rewrite `automation/test_runner.py`**

Replace the whole file with:

```python
"""TestRunner - compatibility facade over the job engine (Stage-1 shim).

Preserves the public surface main_window and the panels use (start/stop/
pause/resume, progress/complete callbacks, .device) while execution lives in
jobs/engine.py. Callbacks fire on the engine thread - the same threading
semantics as the old worker-thread TestRunner. Deleted in Stage 5 once all
consumers build JobSpecs directly.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional

from ..data.database import Database
from ..data.models import TestSession
from ..jobs.engine import JobEngine
from ..jobs.model import (
    TERMINAL_JOB_STATES,
    JobSnapshot,
    JobSpec,
    JobState,
    PhaseSpec,
)
from .profiles import (
    CycleProfile,
    DischargeProfile,
    SteppedProfile,
    TestProfile,
    TimedProfile,
)


class TestState(Enum):
    """Test execution states."""

    IDLE = auto()
    STARTING = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()
    COMPLETED = auto()
    ERROR = auto()
    VOLTAGE_CUTOFF = auto()
    TIMEOUT = auto()


@dataclass
class TestProgress:
    """Current test progress information."""

    state: TestState
    elapsed_seconds: int = 0
    current_step: int = 0
    total_steps: int = 1
    current_cycle: int = 0
    total_cycles: int = 1
    message: str = ""


def profile_to_spec(profile: TestProfile, battery_name: str, notes: str) -> JobSpec:
    """Translate a legacy TestProfile into a declarative JobSpec."""
    if isinstance(profile, DischargeProfile):
        params = {
            "current_a": profile.current_a,
            "voltage_cutoff": profile.voltage_cutoff,
        }
        if profile.max_duration_s is not None:
            params["max_duration_s"] = profile.max_duration_s
        phases, job_type = (PhaseSpec("discharge", params),), "discharge"
    elif isinstance(profile, CycleProfile):
        phase_list = []
        for cycle in range(profile.num_cycles):
            phase_list.append(
                PhaseSpec(
                    "discharge",
                    {
                        "current_a": profile.current_a,
                        "voltage_cutoff": profile.voltage_cutoff,
                    },
                )
            )
            if cycle < profile.num_cycles - 1:
                phase_list.append(
                    PhaseSpec("rest", {"duration_s": profile.rest_between_cycles_s})
                )
        phases, job_type = tuple(phase_list), "cycle"
    elif isinstance(profile, TimedProfile):
        params = {"current_a": profile.current_a, "duration_s": profile.duration_s}
        if profile.voltage_cutoff is not None:
            params["voltage_cutoff"] = profile.voltage_cutoff
        phases, job_type = (PhaseSpec("timed", params),), "timed"
    elif isinstance(profile, SteppedProfile):
        steps = [[step["current_a"], step["duration_s"]] for step in profile.steps]
        params = {
            "steps": steps,
            "rest_between_steps_s": profile.rest_between_steps_s,
        }
        if profile.voltage_cutoff is not None:
            params["voltage_cutoff"] = profile.voltage_cutoff
        phases, job_type = (PhaseSpec("stepped", params),), "stepped"
    else:
        raise ValueError(f"Unknown profile type: {type(profile).__name__}")
    return JobSpec(
        name=profile.name,
        job_type=job_type,
        phases=phases,
        battery_name=battery_name,
        notes=notes,
    )


class TestRunner:
    def __init__(self, device, database: Database, engine: JobEngine) -> None:
        self.device = device  # panels read .device.is_connected
        self.database = database
        self._engine = engine
        self._job_id: Optional[int] = None
        self._is_cycle_job = False
        self._total_cycles = 1
        self._state = TestState.IDLE
        self._progress = TestProgress(state=TestState.IDLE)
        self._progress_callback: Optional[Callable[[TestProgress], None]] = None
        self._complete_callback: Optional[Callable[[TestSession], None]] = None
        engine.executor.add_snapshot_callback(self._on_snapshot)

    @property
    def state(self) -> TestState:
        return self._state

    @property
    def progress(self) -> TestProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._state in (TestState.STARTING, TestState.RUNNING, TestState.PAUSED)

    def set_progress_callback(self, callback: Callable[[TestProgress], None]) -> None:
        self._progress_callback = callback

    def set_complete_callback(self, callback: Callable[[TestSession], None]) -> None:
        self._complete_callback = callback

    def start(self, profile: TestProfile, battery_name: str = "", notes: str = "") -> bool:
        if self.is_running:
            return False
        if self.device is None or not self.device.is_connected:
            return False
        try:
            spec = profile_to_spec(profile, battery_name, notes)
        except ValueError:
            return False
        try:
            self._job_id = self._engine.submit(spec)
        except RuntimeError:  # safety lockout
            return False
        self._is_cycle_job = isinstance(profile, CycleProfile)
        self._total_cycles = profile.num_cycles if self._is_cycle_job else 1
        self._state = TestState.STARTING
        self._progress = TestProgress(
            state=TestState.STARTING,
            total_steps=len(spec.phases),
            total_cycles=self._total_cycles,
        )
        return True

    def stop(self) -> None:
        if self._job_id is not None:
            self._state = TestState.STOPPING
            self._engine.stop()

    def pause(self) -> None:
        if self._state == TestState.RUNNING:
            self._engine.pause()

    def resume(self) -> None:
        if self._state == TestState.PAUSED:
            self._engine.resume()

    # --- snapshot handling (engine thread) ---

    def _on_snapshot(self, snapshot: JobSnapshot) -> None:
        if snapshot.job_id != self._job_id:
            return
        state = self._map_state(snapshot)
        current_cycle = (
            min(snapshot.phase_index // 2 + 1, self._total_cycles)
            if self._is_cycle_job
            else 1
        )
        self._progress = TestProgress(
            state=state,
            elapsed_seconds=int(snapshot.elapsed_s),
            current_step=snapshot.phase_index + 1,
            total_steps=max(len(snapshot.spec.phases), 1),
            current_cycle=current_cycle,
            total_cycles=self._total_cycles,
            message=snapshot.message,
        )
        self._state = state
        if self._progress_callback:
            try:
                self._progress_callback(self._progress)
            except Exception:
                pass
        if snapshot.state in TERMINAL_JOB_STATES:
            self._finish(snapshot)

    def _map_state(self, snapshot: JobSnapshot) -> TestState:
        if snapshot.state == JobState.RUNNING:
            return TestState.RUNNING
        if snapshot.state == JobState.PAUSED:
            return TestState.PAUSED
        if snapshot.state == JobState.PENDING:
            return TestState.STARTING
        if snapshot.state == JobState.COMPLETED:
            if "voltage_cutoff" in snapshot.message:
                return TestState.VOLTAGE_CUTOFF
            if "timeout" in snapshot.message:
                return TestState.TIMEOUT
            return TestState.COMPLETED
        if snapshot.state == JobState.STOPPED:
            return TestState.COMPLETED  # old stop() also normalized to COMPLETED
        return TestState.ERROR  # FAULTED / INTERRUPTED

    def _finish(self, snapshot: JobSnapshot) -> None:
        job_id, self._job_id = self._job_id, None
        if self._complete_callback is None or job_id is None:
            return
        session = self._last_session(job_id)
        if session is not None:
            try:
                self._complete_callback(session)
            except Exception:
                pass

    def _last_session(self, job_id: int) -> Optional[TestSession]:
        for phase in reversed(self._engine.executor.ledger.get_phases(job_id)):
            if phase.get("session_id"):
                return self.database.get_session(
                    phase["session_id"], include_readings=True
                )
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_test_runner_facade.py -v`
Expected: all PASS (10 tests)

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: everything green. Note: `main_window.py` still constructs `TestRunner(self.device, self.database)` with two args — that call site is fixed in Task 13; nothing imports it during tests, so the suite stays green. Do NOT ship Task 12 without Task 13.

- [ ] **Step 6: Commit**

```bash
git add load_test_bench/automation/test_runner.py tests/test_test_runner_facade.py
git commit -m "Rewrite TestRunner as a facade over the job engine"
```

---

### Task 13: Stage-1 MainWindow wiring, Qt bridge, panel hookup, docs

**Files:**
- Create: `load_test_bench/gui/job_bridge.py`
- Modify: `load_test_bench/gui/main_window.py`
- Modify: `load_test_bench/gui/dp832a_charger_panel.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 4–12.
- Produces: running application with the engine live. `JobEngineBridge(QObject)` with signals `job_changed(object)` and `safety_tripped(str)`.

- [ ] **Step 1: Create `gui/job_bridge.py`**

```python
"""Qt bridge for the job engine - the only file where jobs meets Qt.

Engine callbacks fire on the engine thread; emitting a Signal here queues
delivery onto the GUI thread (CLAUDE.md threading rule).
"""

from PySide6.QtCore import QObject, Signal


class JobEngineBridge(QObject):
    job_changed = Signal(object)  # JobSnapshot
    safety_tripped = Signal(str)  # trip reason

    def __init__(self, executor, parent=None):
        super().__init__(parent)
        executor.add_snapshot_callback(self.job_changed.emit)
```

- [ ] **Step 2: MainWindow — imports and early construction**

Add imports (with the Task 6 jobs imports):

```python
import time
from ..jobs.devices import DeviceRegistry
from ..jobs.engine import JobEngine, JobExecutor
from ..jobs.safety import SafetyConfig, SafetyRules, SafetySupervisor
from .job_bridge import JobEngineBridge
```

(`time` may already be imported — check first. `QLabel`/`QPushButton` are already in the QtWidgets import.)

In `__init__`, directly after `self.notifier = Notifier()`, insert (must exist BEFORE `_create_ui()` because the DP832A panel takes them as constructor args):

```python
        # Job engine collaborators needed by panels (engine itself starts later)
        self.device_registry = DeviceRegistry()
        self.safety_rules = SafetyRules(self._load_safety_config())
        self.safety_supervisor = SafetySupervisor(
            self.safety_rules, on_trip=self._on_safety_trip
        )
```

- [ ] **Step 3: MainWindow — engine block after the recovery hook**

Extend the Task 6 block (after `self._run_startup_recovery()`):

```python
        self.job_executor = JobExecutor(
            ledger=self.job_ledger,
            registry=self.device_registry,
            database=self.database,
            reading_sink=self._enqueue_job_reading,
            supervisor=self.safety_supervisor,
        )
        self.job_engine = JobEngine(self.job_executor)
        self.job_bridge = JobEngineBridge(self.job_executor, parent=self)
        self.job_bridge.safety_tripped.connect(self._show_safety_banner)
        self.job_engine.start()

        # Compatibility facade - created ONCE; device assigned on connect
        self.test_runner = TestRunner(None, self.database, self.job_engine)
        self.test_runner.set_progress_callback(self._on_test_progress)
        self.test_runner.set_complete_callback(self._on_test_complete)
        self.control_panel.test_runner = self.test_runner
        self.battery_capacity_panel.test_runner = self.test_runner
        self.power_bank_panel.test_runner = self.test_runner

        # Safety banner lives in the statusbar (red, hidden until a trip)
        self.safety_banner = QLabel("")
        self.safety_banner.setStyleSheet(
            "color: white; background-color: #b71c1c; padding: 2px 8px; border-radius: 3px;"
        )
        self.safety_banner.hide()
        self.safety_reset_button = QPushButton("Reset Safety Lockout")
        self.safety_reset_button.hide()
        self.safety_reset_button.clicked.connect(self._on_safety_reset)
        self.statusbar.addPermanentWidget(self.safety_banner)
        self.statusbar.addPermanentWidget(self.safety_reset_button)
```

- [ ] **Step 4: MainWindow — replace the per-connect TestRunner construction**

In `_connect_device`, replace these six lines:

```python
        self.test_runner = TestRunner(self.device, self.database)
        self.test_runner.set_progress_callback(self._on_test_progress)
        self.test_runner.set_complete_callback(self._on_test_complete)
        self.control_panel.test_runner = self.test_runner
        self.battery_capacity_panel.test_runner = self.test_runner
        self.power_bank_panel.test_runner = self.test_runner
```

with:

```python
        self.device_registry.register("load", self.device)
        self.test_runner.device = self.device
```

In `_disconnect_device`, next to the existing panel-device clearing, add:

```python
        self.device_registry.unregister("load")
```

Remove the now-stale `self.test_runner = None  # Created after device selection` line from `__init__` (the facade is created in Step 3's block).

- [ ] **Step 5: MainWindow — safety observation and slots**

At the very top of `_on_device_status` (before the `_processing_status` check — safety must never be skipped):

```python
        self.safety_supervisor.observe_load(status, time.monotonic())
```

Add the methods:

```python
    def _load_safety_config(self) -> SafetyConfig:
        """Safety thresholds from settings.json's 'safety' key (defaults otherwise).

        A Settings-dialog Safety tab arrives with Stage 2+; until then the
        file key is the override mechanism.
        """
        try:
            with open(get_data_dir() / "settings.json") as f:
                data = json.load(f).get("safety", {})
        except Exception:
            data = {}
        config = SafetyConfig()
        for key in (
            "mosfet_temp_max_c",
            "ext_temp_max_c",
            "psu_current_max_a",
            "stale_status_timeout_s",
            "temp_hysteresis_c",
        ):
            if key in data:
                setattr(config, key, data[key])
        return config

    def _enqueue_job_reading(self, session_id: int, reading) -> None:
        """Engine-thread reading sink into the existing background DB writer."""
        try:
            self._db_queue.put_nowait((session_id, reading))
        except Exception:
            pass  # queue full - drop rather than block the engine

    def _on_safety_trip(self, reason: str) -> None:
        """Called from a device poll thread - wake the engine, signal the GUI."""
        self.job_engine.wake()
        self.job_bridge.safety_tripped.emit(reason)

    @Slot(str)
    def _show_safety_banner(self, reason: str) -> None:
        self.safety_banner.setText(f"SAFETY TRIP: {reason}")
        self.safety_banner.show()
        self.safety_reset_button.show()

    @Slot()
    def _on_safety_reset(self) -> None:
        if self.safety_supervisor.try_reset():
            self.safety_banner.hide()
            self.safety_reset_button.hide()
            self.statusbar.showMessage("Safety lockout cleared")
        else:
            self.statusbar.showMessage(
                "Cannot reset - safety condition still present"
            )
```

In `closeEvent`, directly before `self.dp832a_charger_panel.shutdown()`:

```python
        self.job_engine.shutdown()
```

- [ ] **Step 6: DP832A panel — registry registration + safety feed**

In `load_test_bench/gui/dp832a_charger_panel.py`:

1. Add `import time` to the imports if absent (it is already imported).
2. Change the constructor signature to `def __init__(self, parent=None, registry=None, supervisor=None):` and store `self._registry = registry` / `self._supervisor = supervisor` before `_create_ui()` is called.
3. Replace `self.charger.set_status_callback(self.charger_status.emit)` with `self.charger.set_status_callback(self._on_poll_status)` and add the method:

```python
    def _on_poll_status(self, status) -> None:
        """Poll-thread hook: safety observation first, then marshal to GUI."""
        if self._supervisor is not None:
            self._supervisor.observe_psu(status, time.monotonic())
        self.charger_status.emit(status)
```

4. In `_on_connect_clicked`, after the successful-connect `self._set_connected_ui(True)`:

```python
        if self._registry is not None:
            self._registry.register("psu", self.charger)
```

and in the disconnect branch (before `self.charger.disconnect()`), plus in `shutdown()`:

```python
        if self._registry is not None:
            self._registry.unregister("psu")
```

5. In `main_window.py` `_create_ui`, change the construction to:

```python
        self.dp832a_charger_panel = DP832AChargerPanel(
            registry=self.device_registry, supervisor=self.safety_supervisor
        )
```

- [ ] **Step 7: Run the full suite**

Run: `uv run --extra dev pytest`
Expected: everything green.

- [ ] **Step 8: Smoke test**

Kill any prior app instance you launched (by task ID), then launch in background: `uv run python -m load_test_bench.main`. Verify from the output: no traceback. In the GUI (if a DL24 is attached, otherwise verify app start only):
- Start a discharge from Battery Capacity → identical behavior to before (progress, stop works, session saved).
- Safety drill (no hardware needed for the config part): add `"safety": {"mosfet_temp_max_c": 1}` to `settings.json` in the data dir, restart with the DL24 attached — the banner must appear within ~2 s of the first status and the load must refuse new tests until "Reset Safety Lockout" (which will refuse while the reading exceeds 1 °C — proving the latch; remove the override afterwards).
Kill the instance when done.

- [ ] **Step 9: Update CLAUDE.md**

Add after the "### Rigol DP832A Charger (LAN)" section:

```markdown
### Job Engine (`load_test_bench/jobs/`)

Durable job execution (spec: `docs/superpowers/specs/2026-07-24-job-engine-design.md`).
The package is Qt-free - it is the testability boundary and the Prefect seam.

- `model.py` - JobSpec/PhaseSpec (declarative, JSON-round-trip), JobState/PhaseState
- `ledger.py` - jobs/job_phases tables in tests.db (PRAGMA user_version migrations
  in `data/database.py`); heartbeat every 5 s while running
- `recovery.py` - startup detect + make-safe: orphaned jobs/sessions finalized as
  INTERRUPTED (data kept, never resumed), outputs forced off best-effort
- `cores.py` / `phases.py` - pure decision FSMs + thin actuation shells;
  new phase types register in `PHASE_TYPES` (domain-neutral engine)
- `safety.py` - actuating SafetySupervisor (over-temp, PSU current ceiling,
  stale-status watchdog); latching; separate from notify-only `alerts/`;
  thresholds via a `safety` key in settings.json
- `engine.py` - thread-free JobExecutor driven by `step(now_s)` (tests use a
  fake clock) + JobEngine daemon thread; ALL device commands during jobs run
  on the engine thread
- `gui/job_bridge.py` - the only Qt↔jobs file
- `automation/test_runner.py` is now a compatibility facade over the engine
  (same public surface; deleted in Stage 5)
```

Update the "Test Coverage" section's total count and file list to what `uv run --extra dev pytest` actually reports after this task (9 new test files: migrations, job_model, job_ledger, devices, recovery, scpi_transport, phase_cores, phases, safety, job_executor, test_runner_facade — adjust wording to the real numbers).

- [ ] **Step 10: Commit**

```bash
git add load_test_bench/gui/job_bridge.py load_test_bench/gui/main_window.py load_test_bench/gui/dp832a_charger_panel.py CLAUDE.md
git commit -m "Wire job engine, safety supervisor, and facade into the application"
```

---

## Execution Notes

- Tasks must run in order (each consumes the previous interfaces). Tasks 12 and 13 are a pair: the app is broken between them (main_window still passes two args to TestRunner) — do not stop between their commits.
- After Task 13, Stage 0 + Stage 1 of the spec are complete. Stage 2 (charge-as-a-job), Stage 3 (cycle UI), Stage 4 (panel ports), Stage 5 (scheduler UI + facade deletion) are separate future plans.

Intentional deviations from the spec (documented, not omissions):
- The safety banner lives in the statusbar as a permanent red widget rather than a layout-inserted strip (smaller GUI surgery; behavior identical).
- Recovery's "notifier ping" is deferred to Stage 2+ — recovery surfaces via the dialog and statusbar for now.
- `UsbScpiLink` is deferred until the HDS200 protocol PDF and hardware exist; only the `ScpiLink` seam ships now.
- The Settings-dialog Safety tab is deferred; thresholds are configurable via the `safety` key in `settings.json` (`_load_safety_config`).
