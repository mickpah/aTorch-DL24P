"""Battery charging panel driving a Rigol DP832A power supply over LAN.

DL24 panels in this app drive an injected device owned by MainWindow. This
panel additionally OWNS its RigolDP832A instance, since the charger is
independent of the DL24 connection. Status callbacks arrive on the poll
thread and are marshalled to the GUI thread via Qt signals.
"""

import json
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ..automation.charge_monitor import DEFAULT_TAPER_SAMPLES, ChargeMonitor, ChargeState
from ..config import get_data_dir
from ..protocol.dp832a_protocol import CHANNEL_LIMITS
from ..protocol.rigol_dp832a import ChargerError, RigolDP832A

OVP_MARGIN_V = 0.1  # OVP armed this far above the charge voltage
MAX_OUTPUT_OFF_RETRIES = 10  # 1 initial attempt + up to 9 retries, 1 s apart
STALE_STATUS_FAULT_TICKS = 5  # consecutive missing-status ticks before declaring a fault


class DP832AChargerPanel(QWidget):
    # Marshal poll-thread callbacks onto the GUI thread
    charger_status = Signal(object)  # ChargerStatus
    charger_error = Signal(str)

    def __init__(self, parent=None, registry=None, supervisor=None):
        super().__init__(parent)
        self._registry = registry
        self._supervisor = supervisor
        self.charger = RigolDP832A()
        self.charger.set_status_callback(self._on_poll_status)
        self.charger.set_error_callback(self.charger_error.emit)

        self._monitor: Optional[ChargeMonitor] = None
        self._loading_settings = False
        self._session_file = get_data_dir() / "sessions" / "dp832a_charger_session.json"
        self._stale_ticks = 0

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        # Retry loop for output_off() failures (lock-busy / I/O error) - see
        # _ensure_output_off().
        self._output_off_attempts = 0
        self._output_off_retry_timer = QTimer(self)
        self._output_off_retry_timer.setSingleShot(True)
        self._output_off_retry_timer.setInterval(1000)
        self._output_off_retry_timer.timeout.connect(self._retry_output_off)

        self._create_ui()
        self._load_session()
        self._connect_save_signals()

        self.charger_status.connect(self._on_charger_status)
        self.charger_error.connect(self._on_charger_error)

    def _on_poll_status(self, status) -> None:
        """Poll-thread hook: safety observation first, then marshal to GUI."""
        if self._supervisor is not None:
            self._supervisor.observe_psu(status, time.monotonic())
        self.charger_status.emit(status)

    # --- UI construction ---

    def _create_ui(self) -> None:
        layout = QHBoxLayout(self)

        # Connection
        conn_group = QGroupBox("DP832A Connection (LAN)")
        conn_layout = QGridLayout(conn_group)
        conn_layout.addWidget(QLabel("IP Address:"), 0, 0)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("192.168.1.100")
        conn_layout.addWidget(self.host_edit, 0, 1)
        conn_layout.addWidget(QLabel("Port:"), 1, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(RigolDP832A.DEFAULT_PORT)
        conn_layout.addWidget(self.port_spin, 1, 1)
        conn_layout.addWidget(QLabel("Channel:"), 2, 0)
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(["CH1 (30V/3A)", "CH2 (30V/3A)", "CH3 (5V/3A)"])
        conn_layout.addWidget(self.channel_combo, 2, 1)
        self.connect_button = QPushButton("Connect")
        conn_layout.addWidget(self.connect_button, 3, 0, 1, 2)
        self.identity_label = QLabel("Not connected")
        self.identity_label.setWordWrap(True)
        conn_layout.addWidget(self.identity_label, 4, 0, 1, 2)
        layout.addWidget(conn_group)

        # Charge settings
        settings_group = QGroupBox("Charge Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.addWidget(QLabel("Charge Voltage:"), 0, 0)
        self.voltage_spin = QDoubleSpinBox()
        self.voltage_spin.setRange(0.0, CHANNEL_LIMITS[1][0])
        self.voltage_spin.setDecimals(3)
        self.voltage_spin.setSingleStep(0.1)
        self.voltage_spin.setValue(4.2)
        self.voltage_spin.setSuffix(" V")
        settings_layout.addWidget(self.voltage_spin, 0, 1)
        settings_layout.addWidget(QLabel("Charge Current:"), 1, 0)
        self.current_spin = QDoubleSpinBox()
        self.current_spin.setRange(0.001, CHANNEL_LIMITS[1][1])
        self.current_spin.setDecimals(3)
        self.current_spin.setSingleStep(0.1)
        self.current_spin.setValue(1.0)
        self.current_spin.setSuffix(" A")
        settings_layout.addWidget(self.current_spin, 1, 1)
        settings_layout.addWidget(QLabel("Term. Current:"), 2, 0)
        self.term_current_spin = QDoubleSpinBox()
        self.term_current_spin.setRange(0.001, CHANNEL_LIMITS[1][1])
        self.term_current_spin.setDecimals(3)
        self.term_current_spin.setSingleStep(0.01)
        self.term_current_spin.setValue(0.05)
        self.term_current_spin.setSuffix(" A")
        self.term_current_spin.setToolTip(
            f"Charge ends when CV-mode current stays below this for "
            f"{DEFAULT_TAPER_SAMPLES} consecutive samples"
        )
        settings_layout.addWidget(self.term_current_spin, 2, 1)
        settings_layout.addWidget(QLabel("Safety Timeout:"), 3, 0)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 48)
        self.timeout_spin.setValue(8)
        self.timeout_spin.setSuffix(" h")
        settings_layout.addWidget(self.timeout_spin, 3, 1)
        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start Charge")
        self.start_button.setEnabled(False)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        settings_layout.addLayout(button_row, 4, 0, 1, 2)
        layout.addWidget(settings_group)

        # Live status
        status_group = QGroupBox("Charge Status")
        status_layout = QGridLayout(status_group)
        status_layout.addWidget(QLabel("Voltage:"), 0, 0)
        self.voltage_label = QLabel("--")
        status_layout.addWidget(self.voltage_label, 0, 1)
        status_layout.addWidget(QLabel("Current:"), 1, 0)
        self.current_label = QLabel("--")
        status_layout.addWidget(self.current_label, 1, 1)
        status_layout.addWidget(QLabel("Power:"), 2, 0)
        self.power_label = QLabel("--")
        status_layout.addWidget(self.power_label, 2, 1)
        status_layout.addWidget(QLabel("Mode:"), 3, 0)
        self.mode_label = QLabel("--")
        status_layout.addWidget(self.mode_label, 3, 1)
        status_layout.addWidget(QLabel("Elapsed:"), 4, 0)
        self.elapsed_label = QLabel("--")
        status_layout.addWidget(self.elapsed_label, 4, 1)
        self.state_label = QLabel("Idle")
        self.state_label.setAlignment(Qt.AlignCenter)
        status_layout.addWidget(self.state_label, 5, 0, 1, 2)
        layout.addWidget(status_group)

        layout.addStretch()

        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)

    # --- connection ---

    @Slot()
    def _on_connect_clicked(self) -> None:
        if self.charger.is_connected:
            self._stop_if_charging()
            self._output_off_retry_timer.stop()
            if self._registry is not None:
                self._registry.unregister("psu")
            self.charger.disconnect()
            self._set_connected_ui(False)
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "Charger", "Enter the DP832A IP address or hostname.")
            return
        try:
            self.charger.set_channel(self.channel_combo.currentIndex() + 1)
            self.charger.connect(host, self.port_spin.value())
        except ChargerError as e:
            QMessageBox.critical(self, "Charger", str(e))
            return
        self.identity_label.setText(self.charger.identity)
        self._set_connected_ui(True)
        if self._registry is not None:
            self._registry.register("psu", self.charger)

    @Slot(int)
    def _on_channel_changed(self, index: int) -> None:
        channel = index + 1
        max_v, max_a = CHANNEL_LIMITS[channel]
        self.voltage_spin.setMaximum(max_v)
        self.current_spin.setMaximum(max_a)
        self.term_current_spin.setMaximum(max_a)
        if self.charger.is_connected:
            self.charger.set_channel(channel)
        self._on_settings_changed()

    # --- charge lifecycle ---

    @Slot()
    def _on_start_clicked(self) -> None:
        if not self.charger.is_connected:
            QMessageBox.warning(self, "Charger", "Connect to the DP832A first.")
            return
        self._output_off_retry_timer.stop()
        volts = self.voltage_spin.value()
        amps = self.current_spin.value()
        ok = (
            self.charger.set_voltage(volts)
            and self.charger.set_current(amps)
            and self.charger.set_ovp(volts + OVP_MARGIN_V)
            and self.charger.output_on()
        )
        if not ok:
            QMessageBox.warning(
                self, "Charger", "Failed to start charge (device busy or unreachable)."
            )
            return
        self._monitor = ChargeMonitor(
            termination_current_a=self.term_current_spin.value(),
            timeout_s=self.timeout_spin.value() * 3600.0,
        )
        self._monitor.start(time.monotonic())
        self._stale_ticks = 0
        self._set_charging_ui(True)
        self.state_label.setText("Charging…")
        self.elapsed_label.setText("00:00:00")
        self._tick_timer.start()

    @Slot()
    def _on_stop_clicked(self) -> None:
        if self._stop_if_charging():
            self.state_label.setText("Stopped by user")

    def _stop_if_charging(self) -> bool:
        """Tear down charging state. Returns True once the output is confirmed off.

        On False, _ensure_output_off() is already retrying (or gave up) and
        has put its own warning on state_label - callers should not overwrite
        it with a "stopped" message.
        """
        was_charging = self._monitor is not None and self._monitor.state == ChargeState.CHARGING
        self._monitor = None
        self._tick_timer.stop()
        self._set_charging_ui(False)
        if was_charging:
            return self._ensure_output_off()
        return True

    @Slot()
    def _on_tick(self) -> None:
        if not self._monitor or self._monitor.state != ChargeState.CHARGING:
            return
        now = time.monotonic()
        self.elapsed_label.setText(self._format_elapsed(self._monitor.elapsed_s(now)))
        status = self.charger.last_status
        if status is None:
            self._stale_ticks += 1
            if self._stale_ticks >= STALE_STATUS_FAULT_TICKS:
                self._finish_charge(
                    "Charge stopped: lost contact with charger - output state "
                    "unknown, check the instrument"
                )
            return
        self._stale_ticks = 0
        state = self._monitor.update(status, now)
        if state == ChargeState.COMPLETE:
            self._finish_charge("Charge complete (current tapered below cutoff)")
        elif state == ChargeState.TIMED_OUT:
            self._finish_charge("Charge stopped: safety timeout reached")
        elif state == ChargeState.FAULT:
            self._finish_charge("Charge stopped: output turned off unexpectedly")

    def _finish_charge(self, message: str) -> None:
        self._monitor = None
        self._tick_timer.stop()
        self._set_charging_ui(False)
        if self._ensure_output_off():
            self.state_label.setText(message)

    # --- output-off retry (fix for silently-ignored output_off() failure) ---

    def _ensure_output_off(self) -> bool:
        """Turn the charger output off, retrying on failure without blocking the GUI.

        Returns True if the output is confirmed off on this call. On False a
        1 s single-shot retry loop has been started (up to
        MAX_OUTPUT_OFF_RETRIES total attempts) and state_label shows a
        warning; if retries are exhausted or the charger is disconnected, the
        warning is left showing for the user to act on.
        """
        self._output_off_attempts = 0
        return self._attempt_output_off()

    def _attempt_output_off(self) -> bool:
        if not self.charger.is_connected:
            return False
        self._output_off_attempts += 1
        if self.charger.output_off():
            if self._output_off_attempts > 1:
                self.state_label.setText(f"{self.state_label.text()} (output off after retry)")
            return True
        if self._output_off_attempts >= MAX_OUTPUT_OFF_RETRIES:
            self.state_label.setText("WARNING: could not turn charger output off - turn the channel off on the instrument front panel")
            return False
        self.state_label.setText(
            "WARNING: failed to turn charger output off - retrying... "
            "If this persists, turn the channel off on the instrument front panel."
        )
        self._output_off_retry_timer.start()
        return False

    @Slot()
    def _retry_output_off(self) -> None:
        self._attempt_output_off()

    # --- status display (GUI thread, via signals) ---

    @Slot(object)
    def _on_charger_status(self, status) -> None:
        if not self.charger.is_connected:
            # A queued signal from the poll thread's final iteration can arrive
            # after disconnect already cleared the labels - don't repopulate them.
            return
        self.voltage_label.setText(f"{status.voltage_v:.3f} V")
        self.current_label.setText(f"{status.current_a:.3f} A")
        self.power_label.setText(f"{status.power_w:.3f} W")
        self.mode_label.setText(status.mode if status.output_on else "OFF")

    @Slot(str)
    def _on_charger_error(self, message: str) -> None:
        self.state_label.setText(message)

    # --- UI state helpers ---

    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.host_edit.setEnabled(not connected)
        self.port_spin.setEnabled(not connected)
        self.start_button.setEnabled(connected)
        if not connected:
            self.identity_label.setText("Not connected")
            self.stop_button.setEnabled(False)
            for label in (
                self.voltage_label,
                self.current_label,
                self.power_label,
                self.mode_label,
                self.elapsed_label,
            ):
                label.setText("--")
            self.state_label.setText("Idle")

    def _set_charging_ui(self, charging: bool) -> None:
        self.start_button.setEnabled(not charging and self.charger.is_connected)
        self.stop_button.setEnabled(charging)
        self.connect_button.setEnabled(not charging)
        self.channel_combo.setEnabled(not charging)
        for spin in (self.voltage_spin, self.current_spin, self.term_current_spin, self.timeout_spin):
            spin.setEnabled(not charging)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

    # --- session persistence (CLAUDE.md Test Automation panel pattern) ---

    def _connect_save_signals(self) -> None:
        self.host_edit.editingFinished.connect(self._on_settings_changed)
        self.port_spin.valueChanged.connect(self._on_settings_changed)
        self.voltage_spin.valueChanged.connect(self._on_settings_changed)
        self.current_spin.valueChanged.connect(self._on_settings_changed)
        self.term_current_spin.valueChanged.connect(self._on_settings_changed)
        self.timeout_spin.valueChanged.connect(self._on_settings_changed)
        # channel_combo saves via _on_channel_changed

    def _on_settings_changed(self) -> None:
        if not self._loading_settings:
            self._save_session()

    def _save_session(self) -> None:
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_file, "w") as f:
                json.dump(
                    {
                        "host": self.host_edit.text(),
                        "port": self.port_spin.value(),
                        "channel": self.channel_combo.currentIndex() + 1,
                        "voltage": self.voltage_spin.value(),
                        "current": self.current_spin.value(),
                        "termination_current": self.term_current_spin.value(),
                        "timeout_hours": self.timeout_spin.value(),
                    },
                    f,
                    indent=2,
                )
        except OSError:
            pass

    def _load_session(self) -> None:
        if not self._session_file.exists():
            return
        try:
            with open(self._session_file) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self._loading_settings = True
        try:
            self.host_edit.setText(data.get("host", ""))
            self.port_spin.setValue(data.get("port", RigolDP832A.DEFAULT_PORT))
            channel = data.get("channel", 1)
            if channel not in (1, 2, 3):
                channel = 1
            self.channel_combo.setCurrentIndex(channel - 1)
            self.voltage_spin.setValue(data.get("voltage", 4.2))
            self.current_spin.setValue(data.get("current", 1.0))
            self.term_current_spin.setValue(data.get("termination_current", 0.05))
            self.timeout_spin.setValue(data.get("timeout_hours", 8))
        finally:
            self._loading_settings = False

    # --- app shutdown ---

    def shutdown(self) -> None:
        """Stop any active charge and disconnect. Called from MainWindow.closeEvent.

        The app is exiting, so there's no time (or event loop) left for the
        1 s QTimer retry loop _ensure_output_off() normally uses - retry
        synchronously a couple of times instead, then disconnect regardless.
        """
        if self._output_off_retry_timer.isActive():
            self._output_off_retry_timer.stop()
        was_charging = self._monitor is not None and self._monitor.state == ChargeState.CHARGING
        self._monitor = None
        self._tick_timer.stop()
        if was_charging:
            for attempt in range(3):
                if self.charger.output_off():
                    break
                if attempt < 2:
                    time.sleep(0.5)
        if self._registry is not None:
            self._registry.unregister("psu")
        self.charger.disconnect()
