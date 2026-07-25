# TODO

## Pending Features (prioritized)

Priority tiers, most important first. P0 = do before relying on the newly
merged features on real hardware; P1 = highest-value next work; P2 = depth and
robustness; P3 = polish / speculative. Last reprioritized 2026-07-25 after the
job engine (Stage 0-1) and SCPI voltage meter merged.

### P0 — Verify before real use

- **HDS200 hardware bring-up & verification** — the SCPI voltage meter shipped
  and unit-tested, but its behavior against a real OWON HDS200 is unconfirmed.
  Verify on the bench: the DMM command sequence (`:DMM:CONFigure:VOLTage DC`,
  `:DMM:AUTO ON`, `:DMM:MEAS?`), the real `*IDN?` string (adjust the `hds200`
  profile's `idn_contains`, or fall back to the `generic_scpi_dmm` profile if it
  doesn't match), the USB serial port/baud, and the measure-response format that
  `parse_measurement` sees. Then confirm the cable-drop cutoff actually governs:
  with the meter enabled-for-cutoff, a discharge should run until the *true*
  battery voltage hits the target (device backstop = target − 0.5 V should not
  fire first). Blocks trusting the meter feature end to end.
- **Meter measurement plausibility clamp** — reject SCPI overrange sentinels
  (e.g. `9.9E+37`) and out-of-range values in `parse_measurement` (e.g. clamp to
  0–1000 V) so garbage never lands in `aux_voltage_v` or acts as an
  always-above-cutoff override. Cheap; do it with the P0 bring-up.

### P1 — Highest-value next

- **Safety-trip push notifications** (spec §5.5 step 4) — an over-temp / stale /
  over-current trip on an unattended run currently only cuts outputs + shows a
  GUI banner; fire `alerts/notifier.py` (ntfy/pushover) with the confirmed
  output state. Highest-value safety gap for a battery-heating rig left running.
- **Charge → rest → discharge cycle testing** (DP832A + DL24 coordination) — the
  headline capability the job engine was built to enable, now unblocked. Add a
  cycle-test UI that builds charge/rest/discharge JobSpecs.
- **Complete meter coverage** — the meter today only feeds the engine/facade
  discharge path (Battery Capacity, Power Bank). Extend cutoff + aux logging to:
  the pre-engine panel sweeps (Battery Load, Charger Load — arrives when they
  migrate to the job engine), and the manual DL24 control-panel logging path.
- **`JobEngine.shutdown()` join-timeout overlap** — if the 5 s join times out,
  the final synchronous make-safe step can overlap a still-running engine
  thread; check `is_alive()` after join and skip/warn instead.
- **Database Schema Overhaul** — the migration framework now exists
  (`PRAGMA user_version` + `_MIGRATIONS` in `database.py`, added by the job
  engine), so the mechanism is no longer the blocker. Remaining work:
  - Reconcile `sessions`/`readings` structure with the current logging pipeline
    (bounded deque, commit batching, engine job_phases, per-test-type fields)
  - Audit which fields are actually populated vs left empty/stale across panels
  - Align DB storage with the JSON export schema
  - Migration path for existing `tests.db` files (now straightforward via the
    versioned migration list)

### P2 — Depth & robustness

- **Charge-curve plotting + DB logging of charge sessions** — the DP832A
  "Battery Charging" tab is manual control only; log readings + plot the curve
  (needs a charge session flowing through the DB pipeline).
- **Async (non-blocking) connect** — meter connect blocks the GUI thread up to
  ~4 s (socket + `*IDN?`), `RigolDP832A.connect()` up to ~2 s. Move off the GUI
  thread so an unreachable host at startup doesn't stall the app.
- **Pre-test reset sequence, consistent across panels** — before each test:
  load off → reset counters (mAh, Wh, time) → wait 5 s → start, with a countdown
  in the status label ("Preparing… 5s"). The engine phases already reset
  counters on enter; unify the panel-driven paths to match.
- **TimedPhase device-timer backstop** — re-arm the device hardware timer as a
  device-side stop (the old TestRunner used `set_timer`); lost when the phase
  model replaced it.
- **Battery chemistry presets for charge voltage/current** (DP832A charging).
- **`UsbScpiLink` write timeout** — set a serial `write_timeout` so a wedged CDC
  device can't park the poll thread in `write()` until disconnect.
- **Meter "enabled" gating** — connecting the meter registers + logs it even when
  the "enable" checkbox is unchecked (enabled currently gates only autoconnect +
  cutoff). Document the "connected ⇒ logged" behavior, or gate registration on it.

### P3 — Polish & speculative

- **Standardize reading parameter naming** (larger cleanup):
  - Clean up names in `DeviceStatus` and throughout (voltage vs V, current vs I,
    capacity_mah vs capacity)
  - Expose device-provided parameters not currently surfaced
  - Update the JSON export schema to standardized names (with back-compat for
    existing JSON files) and document conventions in CLAUDE.md
- **Export / data**: Excel export improvements; gzip-compressed JSON
  (`.json.gz`, ~70–90% smaller via stdlib `gzip`); historical comparison/overlay.
- **UI tweaks**: move the ON/OFF status indicator next to the Load on/off switch;
  clean up parameter naming/units in the control-status area above the plot.
- **Custom instrument-profile authoring UI** — meter profiles are code-defined
  today; a UI would let users add SCPI instruments without editing code.
- **Closed-loop charge-voltage compensation** against the meter (trim the PSU
  setpoint to hit the true battery voltage) — complex, speculative.
- **Facade lost-stop window** — a stop clicked between `start()` returning and the
  engine's pickup tick is discarded; clear `_stop_requested` in `_finish_job`
  instead of at pickup. Moot once the TestRunner facade is removed (Stage 5).
- **PyInstaller**: retire the duplicate `pyinstaller` runtime dependency (belongs
  only in the `dev` extra — trivial); test the Windows build; consider macOS code
  signing.

---

## Known Issues

### Bluetooth Communication Not Working
- **Issue**: DL24P connects via Bluetooth SPP but doesn't respond to commands
- **Tested protocols**:
  - Atorch protocol (`FF 55 ...`) - commands sent, no response
  - PX100 protocol (`B1 B2 ...`) - queries sent, no response
- **Port detected**: `/dev/cu.DL24_SPP` (macOS Bluetooth SPP)
- **Possible causes**:
  - Device may use proprietary protocol for Bluetooth (official app only)
  - Bluetooth module may need unknown initialization sequence
  - May only support one-way communication (app -> device)
- **Current state**: USB HID works perfectly; Bluetooth disabled in UI
- **Workaround**: Use USB HID connection (primary supported method)
- **Next step**: Capture Bluetooth traffic from official iOS app using Apple's PacketLogger to reverse-engineer the protocol

### Display Precision vs USB Protocol Precision
- **Issue**: Device screen shows more precision than USB protocol transmits
- **Current state**: Device transmits integer values via USB HID:
  - Current: integer mA (e.g., 49 mA, not 49.123 mA)
  - Power: integer mW (calculated from V×I)
  - Energy: integer mWh (e.g., 2 mWh, not 1.84 mWh)
- **Device screen**: Shows calculated values with more precision (e.g., 1.84 mWh)
- **App display**: Shows integer values with .000 decimal places (e.g., 2.000 mWh)
- **Root cause**: DL24P firmware rounds to integers before USB transmission
- **Possible improvements**:
  - Calculate energy locally from accumulated V×I×time for more precision
  - Interpolate between readings for smoother display
  - Add option to show calculated vs device-reported values
  - Document limitation in user guide
- **Note**: Saved data (JSON, CSV) uses same precision as USB protocol

### Battery Resistance Protocol Parsing
- **Issue**: Battery internal resistance value fluctuates more than expected when reading from device protocol
- **Current location**: Offset 36-37 in counters response (sub-cmd 0x05), uint16 big-endian, milli-ohms
- **Problem**: The bytes overlap with MOSFET temperature (offset 36-39 as uint32 LE)
  - When temp low byte is 0x05: reads as 1380 mΩ (correct, matches device screen)
  - When temp low byte is 0xe6: reads as 58980 mΩ (invalid, device screen shows 1300-1400 mΩ)
- **Validation added**: Only accept values in 1000-2000 mΩ range, ignore others
- **Current workaround**: Using calculated method (R_total - R_load) instead of device value
  - R_total = V / I (total circuit resistance)
  - R_load from device at offset 16-17
  - R_battery = R_total - R_load
- **Device screen shows**: 1300-1400 mΩ stable range (1380 mΩ typical)
- **Next steps**:
  - Investigate if battery R is stored at a different offset
  - Check if there's a different encoding or data packing scheme
  - Monitor more payload samples to find consistent storage location
  - May need to capture USB traffic when battery R changes significantly

---

## Resolved Issues

### SCPI Voltage Meter - DONE (2026-07-25)
- Optional SCPI DMM (OWON HDS200 over USB, or any SCPI voltmeter over USB/LAN via
  profiles) senses true battery-terminal voltage to mitigate cable IR drop
- `aux_voltage_v` logged with every engine reading, exported to CSV/JSON/Excel
- Meter voltage can source discharge cutoff (Battery Capacity / Power Bank); the
  device hardware cutoff drops to a crash-only backstop so the meter governs;
  conservative fallback to the load's voltage when the meter drops out
- Device → "Voltage Monitor…" dialog; settings persist under the `meter` key
- Follow-ups tracked above (P0 hardware bring-up, P1 coverage completion)

### Durable Job Engine, Stage 0-1 - DONE (2026-07-25)
- One Qt-free engine replacing the hand-rolled test runners behind a compatibility
  facade; declarative multi-phase jobs (discharge/rest/timed/stepped)
- SQLite job ledger + `PRAGMA user_version` migration framework in `tests.db`
- Startup detect-and-make-safe crash recovery (orphaned runs finalized, outputs
  forced off, never resumed)
- Actuating latching SafetySupervisor (over-temp, PSU current, stale-status);
  thresholds via the `safety` key in settings.json
- Link-agnostic `ScpiTransport` (LAN + USB)
- Follow-ups tracked above (safety notifications, cycle testing, shutdown edge)

### Battery Charging (Rigol DP832A) - DONE (2026-07-24)
- "Battery Charging" tab: CC-CV charging via a DP832A over LAN (SCPI, port 5555)
- Configurable termination current + safety timeout; auto OVP at Vset + 0.1 V
- Output-off retry with front-panel warning; faults on lost network contact
- Follow-ups tracked above (charge-curve logging, chemistry presets, cycle testing)

### Package Rename - DONE (2026-02-19)
- Renamed Python package from `atorch/` to `load_test_bench/`
- All imports, build scripts, pyproject.toml, and tests updated
- Entry point: `run_load_test_bench.py` (was `run_atorch.py`)

### Help System - DONE (2026-02-19)
- Moved from in-app QTextBrowser dialog to standalone HTML opened in system browser
- `resources/help/help.html` with dark mode, table of contents, anchor links
- Help → Connection Troubleshooting opens to #troubleshooting anchor

### Control Panel Mode-Specific Inputs - DONE (2026-02-19)
- After test ends/aborts, only the input for the active mode is re-enabled
- Uses `control_panel._update_mode_controls()` instead of blindly enabling all spinboxes

### All Test Panels Implemented - DONE
- **Battery Capacity** - Constant current discharge with capacity measurement
- **Battery Load** - Stepped load characterization (CC/CR/CP)
- **Battery Charger** - CC-CV charging profile analysis via CV mode simulation
- **Charger Load** - Power adapter output testing with stepped loads
- **Power Bank Capacity** - Full discharge capacity testing with auto-voltage detection

### Auto-Connect on Test Start - DONE
- `_try_auto_connect()` in `main_window.py` works across all test panels
- Automatically connects when Start is clicked if device is detected but not connected

### GUI Freezing During Long Tests - DONE (2026-02-09)
- Signal queue overflow prevention (skip updates if still processing)
- Database commit batching (every 10s instead of per-reading)
- Reduced USB HID polling from 0.5s to 1.0s
- Removed periodic auto-save during acquisition
- Stopped appending to unbounded `_current_session.readings` list
- All data preserved in database and bounded `_accumulated_readings` deque (48h capacity)

### Window Recovery Responsivity - DONE (2026-02-10)
- 1-second lock timeout for GUI-called device methods (fail gracefully vs freeze)
- Debug window only updated when visible (eliminates 21,600+ unnecessary GUI ops/hour)

### Data Directory Migration - DONE (2026-02-18)
- Centralized via `load_test_bench/config.py` → `get_data_dir()`
- macOS: `~/Library/Application Support/Load Test Bench/`
- Legacy `~/.atorch/` auto-migrated on first run
- User presets in `<data_dir>/presets/` (organized by type)

### Test Conditions Save/Load - DONE
- Preset system with Save/Delete buttons across all panels
- Default presets in `resources/` subdirectories
- User presets saved to `<data_dir>/presets/`
- Session state persists across app restarts

### Time Limit Setting - DONE
- Device protocol supports minutes mode (flag=0x02) and hours mode (flag=0x01)
- Device does NOT support combined hours+minutes (limitation of firmware)
- Fixed in `set_discharge_time()` to use correct mode based on hours value
