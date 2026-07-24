# Durable Job Engine Design (and Prefect Evaluation)

**Date:** 2026-07-24
**Status:** Approved design, pre-implementation
**Decision:** Do not adopt Prefect now. Build one durable in-app job engine with Prefect-ready seams.

## 1. Background

The question evaluated: should [Prefect](https://www.prefect.io/) become the state/job
management backend for this app, judged against the documented TODOs and future work?

Current state (verified in code):

- **Three disjoint execution engines**, no shared state model:
  1. `automation/test_runner.py` `TestRunner` — a `threading.Thread` running
     discharge/cycle/timed/stepped loops that poll `device.last_status` at 1 Hz;
     pause/stop via `threading.Event`.
  2. Panel QTimer step machines — `gui/battery_load_panel.py` and
     `gui/charger_panel.py` each run stepped sweeps with three plain ints
     (`_current_step`/`_total_steps`/`_step_size`) and a re-armed QTimer,
     driving the device directly on the GUI thread. Untestable as written.
  3. `automation/charge_monitor.py` `ChargeMonitor` — the DP832A CC-CV
     termination FSM (pure logic, unit-tested), ticked by a 1 s QTimer in
     `gui/dp832a_charger_panel.py`.
- **`automation/scheduler.py` is dead code** — an in-memory scheduler imported nowhere.
- **No job state is persisted.** Only measurement data reaches SQLite (a
  `sessions` row at start, `readings` rows per second, `end_time` written on
  graceful finish). A crash mid-test loses all control state, leaves the
  session with `end_time = NULL` (which nothing ever queries), and can leave
  the DL24 load and/or DP832A output **ON**.
- `CycleProfile`/`_run_cycle` is discharge-only — "cycle" today means repeated
  discharge+rest; true charge/discharge cycling does not exist.
- `alerts/` (conditions + notifier) only **notifies** (ntfy/pushover); nothing
  in the app actuates hardware in response to a condition like over-temperature.

## 2. Requirements (user-confirmed)

1. **Charge → rest → discharge cycle testing** coordinating the DP832A charger
   and the DL24 load as multi-phase jobs.
2. **Crash recovery = detect + make safe** — on startup, find orphaned runs,
   force outputs off, finalize sessions as interrupted with data intact.
   **Explicitly no resume** (battery state changes irreversibly; resuming an
   interrupted charge/discharge would be dishonest data and shaky safety).
3. **Unify the three engines** into one job model (also addresses the
   test-lifecycle bug class and the "Database Schema Overhaul" TODO).
4. **Scheduling / unattended runs** (revives what dead `scheduler.py` was for).
5. **Async safety event handling** — e.g. over-temperature → cut load/PSU
   output within ~1–2 s, regardless of which job (or no job) is running.

Deployment direction: heading toward a lab/headless rig eventually; desktop Qt
remains the operator interface for now.

## 3. Prefect Evaluation

### Verdict: not now

Prefect solves durable *task-level* orchestration, scheduling, and remote
observability. Judged against the five requirements:

| Requirement | Prefect fit |
|---|---|
| Cycling + unification | Orchestrates at task granularity; the 1 s hardware control loops, device lock discipline, and second-level state live **below** its abstraction. The local engine must be built either way — Prefect would only wrap it. |
| Detect + make-safe recovery | Prefect crash recovery reschedules flow runs; it does not turn hardware outputs off. The needed routine is app-side regardless. |
| Scheduling | A genuine Prefect strength — but a `scheduled_jobs` table plus one check per engine tick covers the need without a server. |
| Safety events | Must never sit behind an orchestration API round-trip. App-side under any architecture. |

Cost side: Prefect brings an async-first server stack (FastAPI, SQLAlchemy,
uvicorn, alembic, pydantic) grafted onto a synchronous Qt app with no asyncio
anywhere today; significant PyInstaller bundling pain; a second database beside
`tests.db`; and zero benefit while devices are process-local and Qt is the
operator interface.

### Adoption criteria (to be documented in `jobs/__init__.py`)

Revisit Prefect when any of these becomes true:

- (a) the rig goes headless and the Qt UI stops being the operator interface;
- (b) more than one rig needs central scheduling/observability;
- (c) cross-machine retry/caching/artifact semantics are needed.

The Phase seam (§6) makes adoption a wrapper, not a rewrite. The
SafetySupervisor stays app-side under every future architecture.

## 4. Architecture Overview

New package `load_test_bench/jobs/` — **no Qt imports anywhere in it**. This is
simultaneously the testability boundary and the Prefect seam boundary.

```
GUI thread          poll threads               engine thread (new, 1)
─────────────       ────────────────────       ─────────────────────────
panels build   ┌──  DL24 status ──► Safety     JobEngine loop:
JobSpecs,      │    DP832A status ─► eval        executor.step(monotonic())
submit/stop ───┤         (pure, µs)  │           wake_event.wait(1.0)
               │              trip → wake ──►  • phase.tick()
JobEngineBridge◄── callbacks (engine thread)   • ALL device commands
  Qt Signals                                   • readings → _db_queue
  (queued)                                     • ledger writes + heartbeat
                                               • make-safe actuation
```

Components:

| Unit | File | One purpose |
|---|---|---|
| Job/phase model | `jobs/model.py` | States, specs, results — frozen dataclasses, JSON-serializable |
| Device protocols | `jobs/devices.py` | Narrow `LoadDevice`/`PsuDevice` Protocols + thread-safe `DeviceRegistry` |
| Phases | `jobs/phases.py` | Pure decision cores + thin actuation shells |
| Engine | `jobs/engine.py` | `JobExecutor` (thread-free) + `JobEngine` (one thread) |
| Safety | `jobs/safety.py` | Pure `SafetyRules` + latching `SafetySupervisor` |
| Ledger | `jobs/ledger.py` | All SQL for jobs/job_phases/scheduled_jobs |
| Recovery | `jobs/recovery.py` | Startup detect + make-safe routine |
| Scheduler | `jobs/scheduler.py` | Persistent schedule check (no own thread) |
| Qt bridge | `gui/job_bridge.py` | The only Qt↔jobs file; queued Signals |

## 5. Component Design

### 5.1 Model (`jobs/model.py`)

```python
class JobState(Enum):    PENDING, RUNNING, PAUSED, COMPLETED, STOPPED, FAULTED, INTERRUPTED
class PhaseState(Enum):  PENDING, RUNNING, COMPLETED, SKIPPED, FAULTED, INTERRUPTED

@dataclass(frozen=True)
class PhaseSpec:
    phase_type: str      # "charge" | "rest" | "discharge" | "timed" | "stepped"
    params: dict         # JSON-serializable; validated by the phase class

@dataclass(frozen=True)
class JobSpec:
    name: str
    job_type: str        # "cycle_test", "discharge", "charge", "stepped_sweep", ...
    phases: tuple[PhaseSpec, ...]
    battery_name: str = ""              # kept for sessions-table compatibility
    notes: str = ""
    metadata: dict = field(default_factory=dict)  # domain-specific, opaque to the engine

@dataclass
class PhaseResult:       # the future Prefect "task return value"
    state: PhaseState
    reason: str          # "voltage_cutoff", "taper_complete", "timeout", "safety_trip", ...
    metrics: dict        # capacity_mah, energy_wh, duration_s, ...
```

Cycles are **expanded at submit time** (charge, rest, discharge × N becomes a
flat phase tuple) so `phase_index` is stable and the ledger is row-per-phase
with no runtime interpretation.

### 5.2 Device protocols (`jobs/devices.py`)

`LoadDevice` and `PsuDevice` `typing.Protocol`s naming exactly the methods the
phases need (`turn_on/turn_off/set_mode/set_current/set_resistance/
set_voltage_cutoff/reset_counters`; `output_on/output_off/set_voltage/
set_current/set_ovp`) plus `is_connected` and `last_status`. `USBHIDDevice` and
`RigolDP832A` already satisfy them — verify exact method names against the
implementations before coding (CLAUDE.md rule).

`DeviceRegistry` (plain thread-safe class) holds the currently connected
device **per role** (`load`, `psu`, `meter`). `MainWindow` registers the DL24
on connect; `DP832AChargerPanel` registers its charger on connect.
**Ownership does not change.** A job needing a missing device fails fast at
start with a clear error. New roles are additive: a Protocol + a registry
slot, no engine changes.

`FakeLoad`/`FakePsu`/`FakeMeter` live in `tests/fakes.py`.

**Meter role (designed now, driver later):** `MeterDevice` Protocol —
`is_connected`, `last_status` (a small `MeterStatus` with `voltage_v`,
optional `current_a`), for a SCPI DMM doing independent voltage sensing.
Anticipated instrument: OWON HDS200-series handheld scope/DMM, SCPI over USB
(see transport extraction below). Discharge/charge phases take an optional `voltage_source` param
(`"device"` default | `"meter"`): when `"meter"` and a meter is registered,
cutoff/termination decisions use the meter voltage, and each reading row also
records it in the new nullable `readings.aux_voltage_v` column (added in
migration 1 so no second migration is needed when the hardware arrives). No
concrete meter driver is built until real hardware exists.

**SCPI transport extraction (LAN and USB):** the DP832A driver currently
bundles generic SCPI plumbing with DP832A-specific commands, and it assumes a
TCP socket — but SCPI instruments arrive over more than one link (the
anticipated meter, an OWON HDS200-series handheld scope/DMM, is **SCPI over
USB**). Extract `protocol/scpi_transport.py` in two layers:

- **`ScpiLink` protocol** — a minimal byte link: `open()`, `close()`,
  `send(bytes)`, `recv() -> bytes`, timeout handling. Implementations:
  `LanScpiLink` (TCP socket, extracted from `rigol_dp832a.py`) and
  `UsbScpiLink` (USB CDC serial via the existing `pyserial` dependency, or
  raw USB bulk if the instrument requires it — decided per instrument from
  its protocol doc). Tests keep using the FakeSocket pattern, which becomes a
  fake `ScpiLink`.
- **`ScpiTransport`** — link-agnostic: line framing/terminators,
  connect + `*IDN?` verification hook, poll thread, lock-timeout command
  pattern, error callbacks.

`RigolDP832A` is refactored onto `ScpiTransport(LanScpiLink)` with public
behavior unchanged (existing tests keep passing). Future SCPI instruments
then need only a protocol builder/parser class + a role-Protocol adapter,
regardless of link. The HDS200 meter driver specifically awaits its protocol
PDF (`docs/HDS200_Series_SCPI_Protocol.pdf` — currently an empty placeholder
that needs re-adding) and the hardware.

**Alternate SCPI loads:** the `LoadDevice` Protocol was checked against
typical SCPI loads (Rigol DL3000, Siglent SDL1000): `:INP ON|OFF`,
`:FUNC CURR|RES|VOLT|POW`, setpoint and measurement commands map directly onto
`turn_on/turn_off/set_mode/set_current/set_resistance` and `last_status`.
Two DL24 features are not universal: device-side voltage cutoff and
accumulated mAh/Wh counters. Cutoff enforcement is already software-side in
`DischargeCore` (device-side cutoff is a bonus, not a dependency); for
counters, the Protocol marks `reset_counters`/accumulators as **optional
capabilities** — when absent, the executor integrates capacity/energy in
software from per-tick readings. Noted here so the Protocol is written with
the split; no SCPI load driver is built until hardware exists.

### 5.3 Phases (`jobs/phases.py`)

Phase = pure decision core + thin actuation shell (the `ChargeMonitor` house
pattern):

```python
class Phase(ABC):
    spec: PhaseSpec
    def on_enter(self, ctx: PhaseContext) -> None: ...   # establish FULL device state, outputs on
    def tick(self, ctx: PhaseContext, now_s: float) -> PhaseTick: ...  # CONTINUE | DONE(result)
    def on_pause(self, ctx) -> None: ...                 # outputs off
    def on_resume(self, ctx) -> None: ...                # re-issue setpoints, outputs on
    def on_exit(self, ctx, reason: str) -> None: ...     # phase-local make-safe

@dataclass
class PhaseContext:      # everything injected; nothing global, nothing Qt
    load: Optional[LoadDevice]
    psu: Optional[PsuDevice]
    report: PhaseReporter               # on_progress(dict), on_reading(reading)
    settle: Callable[[float], None]     # injectable sleep for inter-command settles
```

Pure cores (no I/O, caller-supplied `now_s`, one class each):

- `ChargeCore` — **reuses `ChargeMonitor` unchanged**.
- `DischargeCore` — extracted from `TestRunner._run_discharge`: voltage cutoff,
  device-side load-off detection, max-duration timeout.
- `RestCore` — trivial duration FSM.
- `TimedCore` — from `_run_timed` (hardware timer + software duration check).
- `SteppedCore` — subsumes `TestRunner._run_stepped` **and** both panel QTimer
  machines: min/max/divisions/dwell arithmetic (`divisions + 1` step semantics
  preserved). The core *returns commands* (`SET_VALUE(x)`, `REST`, `DONE`);
  the shell executes them — which is what finally makes the sweep logic
  unit-testable.

**Domain neutrality (non-battery task control):** the engine knows nothing
about batteries. Phase types are registered in a `PHASE_TYPES: dict[str,
type[Phase]]` mapping in `jobs/phases.py`, so a new domain (PSU burn-in,
DC-DC converter characterization, generic instrument sequencing) is added by
writing a new Phase class and registering it — the engine, ledger, scheduler,
bridge, and recovery are untouched. Battery-ness lives only in phase params
(`voltage_cutoff`, `termination_current_a`, ...), `JobSpec.battery_name`
(sessions compatibility) and `JobSpec.metadata`. The safety layer is likewise
device-role-based, not domain-based.

### 5.4 Engine (`jobs/engine.py`)

Two layers, deliberately:

- **`JobExecutor`** — thread-free. `submit(spec)`, `step(now_s)`,
  `pause()/resume()/stop()`, `abort(reason)`. Owns the current job, current
  phase instance, phase transitions, ledger writes, reading capture (reads
  `device.last_status` each step, pushes `(session_id, reading)` to the
  existing `_db_queue`), heartbeat bookkeeping. **Tests drive `step()` with a
  fake clock — no threads.**
- **`JobEngine`** — the thin thread wrapper: one daemon thread looping
  `executor.step(time.monotonic()); wake_event.wait(1.0)`. The wake event lets
  SafetySupervisor / stop / pause interrupt the wait for sub-second reaction.
  Callbacks (`set_job_callback`, `set_progress_callback`) fire on the engine
  thread; the GUI bridge marshals.

One job at a time; FIFO queue = the ledger's PENDING rows (which also means
the queue survives restarts — though startup recovery marks orphaned PENDING
rows INTERRUPTED too: simplest and safest).

Command failures: a device command that times out (`GUI_LOCK_TIMEOUT` pattern)
is retried next tick; 3 consecutive failures → phase FAULTED → make-safe.

**Threading decision (rejected alternatives):** per-job threads (TestRunner
today) add lifecycle/join/race surface for zero benefit on a one-rig app;
QTimer-driven execution on the GUI thread (panels today) puts blocking USB/LAN
I/O on the main thread — disqualified by this app's documented GUI-freeze
history. One persistent engine thread gives a simple ownership rule: **device
commands during jobs happen on the engine thread, full stop.**

### 5.5 Safety (`jobs/safety.py`)

- **`SafetyRules`** (pure): `evaluate(load_status, psu_status, now_s) ->
  list[Trip]`. v1 rules: DL24 MOSFET over-temp, external-probe over-temp, PSU
  over-current ceiling, and a **stale-status watchdog** (no fresh status for N
  seconds while an output is believed on). Thresholds + hysteresis from a
  `SafetyConfig` dataclass (settings dialog gets a Safety tab; defaults:
  `mosfet_temp_max_c=80`, `ext_temp_max_c=60` — disabled when no probe,
  `psu_current_max_a=None` — disabled until the operator configures it,
  `stale_status_timeout_s=10`).
- **`SafetySupervisor`** (thin): invoked from the two existing status
  pipelines — `MainWindow._on_device_status` (DL24 poll thread) and the DP832A
  status callback (LAN poll thread). It only runs the pure evaluation
  (microseconds) and records per-device `last_seen`. On first trip: latch flag
  + reason, then `engine.abort_for_safety(reason)` → wake event. **All
  actuation happens on the engine thread** — a poll thread must never block on
  the *other* device's lock.
- Active even with **no job running** (manual load use): a trip while idle
  still runs make-safe on the engine thread.
- **Latching:** while tripped, the engine refuses new jobs and the scheduler
  skips (recording the skip). GUI shows a persistent red banner with the
  reason. Operator clears via "Reset safety lockout", accepted only when rules
  currently evaluate clean (below threshold minus hysteresis). Resets are
  logged.
- **`alerts/` stays notify-only, untouched** (30 tests preserved). Notify
  rules are user-tunable conveniences; safety rules are conservative actuating
  invariants. Deliberately not unified.

Safety latency budget: status appears in a 1 Hz poll → pure rule fires in the
callback → wake event → engine wakes within ms → `turn_off()`/`output_off()`
(≤1 s lock timeout each, load first). Worst case ≈ poll interval + lock
timeout ≈ 2 s. Meets the requirement.

**Make-safe procedure** (engine thread; used by safety trips, phase faults,
and startup recovery): (1) DL24 `turn_off()` retry ×3 at 1 s spacing; (2) PSU
`output_off()` retry ×3 (the DP832A panel's proven retry pattern, promoted);
(3) persist job → FAULTED + `fault_reason`, finalize phase sessions; (4) fire
`alerts/notifier.py` with confirmed/unconfirmed output state; (5) emit
`safety_tripped`. If a device is unreachable: record "output state unknown"
and say so loudly — same honesty as the documented DP832A failure model.

### 5.6 Ledger & data model (`jobs/ledger.py`, `data/database.py`)

Tables added to **`tests.db`** (framed as installment 1 of the TODO's
"Database Schema Overhaul"):

```sql
CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    state               TEXT NOT NULL DEFAULT 'PENDING',
    job_type            TEXT NOT NULL,
    name                TEXT NOT NULL,
    spec_json           TEXT NOT NULL,
    current_phase_index INTEGER NOT NULL DEFAULT 0,
    heartbeat_at        TEXT,
    fault_reason        TEXT,
    schedule_id         INTEGER REFERENCES scheduled_jobs(id),
    battery_name        TEXT DEFAULT '',
    notes               TEXT DEFAULT ''
);

CREATE TABLE job_phases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    phase_index   INTEGER NOT NULL,
    phase_type    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'PENDING',
    started_at    TEXT,
    finished_at   TEXT,
    session_id    INTEGER REFERENCES sessions(id),
    result_json   TEXT,
    UNIQUE (job_id, phase_index)
);
CREATE INDEX idx_job_phases_job ON job_phases(job_id);

CREATE TABLE scheduled_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    next_run_at       TEXT NOT NULL,
    repeat_interval_s INTEGER,             -- NULL = one-shot
    grace_window_s    INTEGER NOT NULL DEFAULT 3600,
    spec_json         TEXT NOT NULL,
    last_run_job_id   INTEGER REFERENCES jobs(id)
);

ALTER TABLE sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'completed';
    -- 'running' | 'completed' | 'interrupted' | 'faulted'
ALTER TABLE sessions ADD COLUMN job_phase_id INTEGER;
ALTER TABLE readings ADD COLUMN aux_voltage_v REAL;   -- meter role, nullable
```

**Sessions/readings pipeline unchanged.** Each data-producing phase creates
its own `sessions` row on `on_enter`; readings flow through the existing
`_db_queue` background writer. `job_phases.session_id` ↔ `sessions.job_phase_id`
link the two worlds, so the History panel and Test Viewer keep working
untouched — and a cycle test naturally yields one session per discharge
(per-cycle capacity comparison for free). Rest phases produce no session.

**Heartbeat:** the engine writes `heartbeat_at` and commits every 5 s directly
via the ledger (one UPDATE, 0.2 commits/s — negligible against the historical
1-commit/s GUI-freeze problem, and off the main thread). Heartbeats bypass
`_db_queue` because a heartbeat is only useful if durably committed. State
transitions and `current_phase_index` changes commit immediately.

**Migrations:** `PRAGMA user_version` with a linear in-app migration list in
`database.py::_init_db` (each step one transaction). Migration 1 = everything
above, replacing the ad-hoc root-level `migrate_*.py` script pattern. Add a
migration test against a pre-migration fixture DB.

### 5.7 Recovery (`jobs/recovery.py`)

`run_recovery(ledger, database, registry, notifier) -> RecoveryReport`, hooked
into `MainWindow.__init__`:

1. `find_orphans()`: jobs in RUNNING/PAUSED (+ stale PENDING), plus sessions
   with `end_time IS NULL` (including pre-jobs legacy orphans).
2. Finalize: job → INTERRUPTED (`fault_reason='orphaned at startup (last
   heartbeat <t>)'`); RUNNING phases → INTERRUPTED; linked sessions →
   `end_time = last reading timestamp` (fallback: heartbeat),
   `status='interrupted'`. **Nothing deleted — data intact.**
3. Make-safe, best effort, on a background thread (startup never blocks):
   attempt DL24 auto-connect (existing `_try_auto_connect` machinery) →
   `turn_off()`; attempt PSU connect using the host saved in
   `sessions/dp832a_charger_session.json` → `output_off()` → disconnect.
4. Report: `recovery_report` signal → GUI banner/dialog listing what was found
   and whether each output was **confirmed** off; notifier ping if enabled.

No resume — ever (explicit non-goal).

### 5.8 Scheduler (`jobs/scheduler.py`)

Persistent, replaces dead `automation/scheduler.py` (deleted in Stage 5). No
thread of its own: the engine tick runs `SELECT ... WHERE enabled=1 AND
next_run_at <= now LIMIT 1`, expands `spec_json` → JobSpec, submits, advances
(or disables) `next_run_at`. Pure helper `next_run(schedule, now)` for tests.
A schedule due while a job runs queues behind it. On startup, anything overdue
beyond its grace window (default 1 h) is **skipped** to the next interval and
reported — never fire a charge job hours late unattended.

### 5.9 Qt bridge (`gui/job_bridge.py`)

`JobEngineBridge(QObject)` — Signals `job_changed(object)`,
`phase_progress(object)`, `safety_tripped(str)`, `safety_cleared()`,
`recovery_report(object)`. Engine callbacks (engine thread) → `emit` → queued
delivery on the GUI thread. The only file where the jobs world meets Qt.

## 6. Prefect Seam Contract

Documented in `jobs/__init__.py` alongside the adoption criteria. A Phase must:

1. Take inputs only as JSON-serializable `PhaseSpec.params` + injected
   `PhaseContext` (devices, reporter, clock via `now_s`). No globals, no Qt,
   no ledger, no direct DB.
2. Produce only a JSON-serializable `PhaseResult` and reporter calls.
3. Have idempotent edges: `on_enter` establishes full device state from params
   (never relies on prior-phase leftovers); `on_exit` makes its device safe.
4. Keep `tick()` non-blocking (bounded by one device command timeout); long
   waits are expressed by returning CONTINUE.

Mapping for later adoption: JobSpec → flow (parameters = spec_json); Phase →
`@task run_phase(spec: dict) -> dict` wrapped in a
`while CONTINUE: sleep(1)` loop; JobState → Prefect flow states; the
`scheduled_jobs` table → deployments/schedules; the ledger → superseded by the
Prefect API (or kept as a local mirror). The SafetySupervisor stays app-side
— an orchestrator must never be in the emergency-stop path.

## 7. Staged Migration (each stage leaves the app fully working)

| Stage | Content | Value shipped |
|---|---|---|
| 0 | DB migration 1; `model.py`, `ledger.py`, `recovery.py` + startup hook | Interrupted runs become visible + made safe — before any engine work |
| 1 | `engine.py`, `devices.py` (incl. `ScpiTransport` extraction + `RigolDP832A` refactor), discharge/timed/stepped/rest phases, SafetySupervisor; `test_runner.py` becomes a thin facade (same `start/stop/pause/resume/is_running` + callback surface, `TestProfile`→`JobSpec`) so `main_window.py` and the control/capacity/power-bank panels work unmodified | One engine; safety cutouts live; future SCPI instruments become thin protocol classes |
| 2 | `ChargePhase` wraps ChargeMonitor; DP832A panel submits a single-phase charge job (its `_on_tick`/`_ensure_output_off` logic moves into the phase/engine); panel keeps connection UI + readout | Charging under the same job model |
| 3 | Cycle-test UI building charge→rest→discharge×N JobSpecs | **The documented future work — now possible** |
| 4 | Port `battery_load_panel.py`, then `charger_panel.py`, to stepped-sweep jobs; delete their QTimer machinery | Panels shrink to spec-builders + progress renderers |
| 5 | Scheduler UI; delete `automation/scheduler.py`; delete the TestRunner facade once all consumers are ported | Unattended runs; cleanup complete |

## 8. Testing Strategy (house style: pure logic, injected clocks, no mocks, no GUI tests)

- `tests/test_phase_cores.py` — cores driven by synthesized
  `DeviceStatus`/`ChargerStatus` + hand-fed `now_s` (mirror of
  `test_charge_monitor.py`): cutoff edges, device-side stop, dwell/step
  arithmetic, timeouts.
- `tests/test_safety.py` — threshold crossing, hysteresis, stale-status
  watchdog, latching, reset-only-when-clean.
- `tests/test_job_executor.py` — `JobExecutor.step()` with `FakeLoad`/`FakePsu`
  + fake clock, no threads: full cycle happy path; pause/resume re-issues
  setpoints; stop mid-phase runs `on_exit`; safety abort → FAULTED + both
  fakes off; command-failure retry → fault.
- `tests/test_job_ledger.py`, `tests/test_recovery.py` — tmp-path DBs: submit
  → simulated crash (stop writing) → new ledger → orphans found → INTERRUPTED
  with sessions finalized; migration test against a v0 fixture DB.
- `tests/test_job_scheduler.py` — `next_run()` math, grace-window skip,
  disabled schedules.
- The `JobEngine` thread wrapper stays a few lines and untested-by-design.

## 9. Non-Goals

- No resume-after-crash (detect + make-safe only — user-confirmed).
- No asyncio; threads + Qt only.
- No Prefect now (criteria in §3); no distributed/multi-rig execution.
- No parallel jobs (one rig, one active job, FIFO queue).
- No replacement of the sessions/readings pipeline, `_db_queue` writer, JSON
  export, or the notify-only alerts stack.
- No headless mode yet — the jobs package merely stays Qt-free so headless
  becomes a wrapper, not a rewrite.
- No pyvisa/VISA, no instrument capability discovery, no generic "add
  instrument" UI, and no concrete drivers for hardware not yet owned (meter
  and alternate-load drivers are written when the instruments exist; only
  their seams — Protocols, registry slots, `aux_voltage_v`, transport — are
  built now).

## 10. Files

**Create:** `load_test_bench/jobs/{__init__,model,devices,phases,engine,safety,ledger,recovery,scheduler}.py`,
`load_test_bench/protocol/scpi_transport.py`,
`load_test_bench/gui/job_bridge.py`,
`tests/{fakes,test_phase_cores,test_safety,test_job_executor,test_job_ledger,test_recovery,test_job_scheduler}.py`

**Modify:** `load_test_bench/data/database.py` (user_version migrations, new
tables, sessions/readings columns), `load_test_bench/protocol/rigol_dp832a.py`
(refactor onto `ScpiTransport`, behavior unchanged),
`load_test_bench/gui/main_window.py` (registry, bridge, recovery hook, safety
observe in `_on_device_status`), `load_test_bench/automation/test_runner.py`
(facade; deleted in Stage 5), `load_test_bench/gui/dp832a_charger_panel.py`
(Stage 2), later `gui/battery_load_panel.py`, `gui/charger_panel.py`,
`gui/settings_dialog.py` (Safety tab).

**Delete:** `load_test_bench/automation/scheduler.py` (Stage 5).

**Reuse unchanged:** `ChargeMonitor`, the drivers' `GUI_LOCK_TIMEOUT` command
methods, the `_db_queue` background writer, `alerts/notifier.py`, and the
DP832A panel's output-off retry pattern (promoted into make-safe).
