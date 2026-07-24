# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Changes prior to this file's introduction are recorded in the git history.

## [Unreleased]

### Added
- **Durable job engine (Stage 0 + 1)** — one Qt-free execution engine
  (`load_test_bench/jobs/`) replacing the hand-rolled test runners behind a
  compatibility facade: declarative multi-phase jobs (discharge/rest/timed/
  stepped), a SQLite job ledger in `tests.db` with 5 s heartbeats, and a
  schema migration framework (`PRAGMA user_version`, transactional)
- **Startup crash recovery** — orphaned runs are detected on launch,
  finalized as interrupted with all data kept (never resumed), and device
  outputs are forced off best-effort, with a recovery report dialog
- **Actuating safety supervisor** — over-temperature (MOSFET + external
  probe), PSU current ceiling, and stale-status watchdog rules that cut
  hardware outputs and latch a lockout until the operator resets (separate
  from the notify-only alerts); thresholds configurable via a `safety` key
  in `settings.json`
- **Link-agnostic SCPI transport** (`protocol/scpi_transport.py`) — the
  DP832A driver now rides on a reusable LAN link layer; a USB link slots in
  when the OWON HDS200 meter driver lands (protocol PDF added under `docs/`)
- Design documents: Prefect evaluation (verdict: not now, criteria recorded)
  and the job engine design spec + staged implementation plan under
  `docs/superpowers/`
- ~110 new tests (schema migrations, job model/ledger, device fakes,
  recovery, SCPI transport, phase cores/shells, safety, executor, facade)

- **Battery Charging tab** — charge batteries from a Rigol DP832A bench power
  supply over its LAN interface (raw SCPI on TCP port 5555, stdlib socket, no
  VISA required): channel selection (CH1/CH2 30 V/3 A, CH3 5 V/3 A), CC-CV
  charging with configurable termination current and safety timeout, automatic
  OVP at charge voltage + 0.1 V, live voltage/current/power readout, and
  session persistence
- Charging safety hardening: failed output-off commands retry with a visible
  front-panel warning, and lost network contact mid-charge faults the charge
  instead of trusting stale readings
- 34 new tests covering the DP832A SCPI protocol, LAN driver (via fake
  socket), and CC-CV charge termination state machine (152 tests total)
- `justfile` with common development tasks — run `just` to list them
  (`just run`, `just viewer`, `just test`, `just build`, `just sync`, ...)
- C/2 and C/5 discharge rate buttons in the control panel
- Start delay setting applied consistently before tests, with a pre-test
  reset sequence and plot time scaling
- DL24P power connector specifications in the README

### Changed
- Dependency management migrated to [uv](https://docs.astral.sh/uv/)
  (`pyproject.toml` + `uv.lock`); `requirements.txt` is legacy
- "V Cutoff" label renamed to "Cutoff" with a V suffix on the spinbox
- Test logging decoupled from the manual log switch

### Fixed
- DP832A driver: reconnecting after changing the host no longer silently
  dials the previously connected instrument
- Paused tests no longer count wall-clock pause time against duration limits
  or step dwell times
- Debug log path no longer hardcoded to a developer machine — it now resolves
  relative to the repository
- Time limit and discharge time handling during tests
- Test lifecycle issues (status label stuck on countdown text, double-triggered
  USB prepare/reconnect flow)
