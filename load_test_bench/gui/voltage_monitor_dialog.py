"""Dialog to configure and connect the optional SCPI voltage meter.

The meter senses true battery-terminal voltage to mitigate cable IR drop.
Settings persist in settings.json (see config.MeterSettings). The dialog owns
no device state - it drives the shared ScpiMeter passed in by MainWindow.
"""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_data_dir, load_meter_settings, save_meter_settings, MeterSettings
from ..protocol.meter_protocol import METER_PROFILES
from ..protocol.scpi_meter import MeterError
from ..protocol.scpi_transport import list_serial_ports


class VoltageMonitorDialog(QDialog):
    def __init__(self, parent, meter, on_settings_changed):
        super().__init__(parent)
        self.setWindowTitle("Voltage Monitor (SCPI Meter)")
        self._meter = meter
        self._on_settings_changed = on_settings_changed
        self._settings_file = get_data_dir() / "settings.json"

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Optional: sense true battery-terminal voltage with an SCPI meter "
            "to mitigate cable voltage drop. When enabled, the meter voltage is "
            "logged with every reading and (optionally) used for discharge cutoff."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.enabled_check = QCheckBox("Enable voltage meter")
        form.addRow(self.enabled_check)

        self.profile_combo = QComboBox()
        for key, profile in METER_PROFILES.items():
            self.profile_combo.addItem(profile.label, key)
        form.addRow("Instrument:", self.profile_combo)

        self.transport_combo = QComboBox()
        self.transport_combo.addItem("USB (serial)", "usb")
        self.transport_combo.addItem("LAN (TCP)", "lan")
        form.addRow("Transport:", self.transport_combo)

        self.serial_combo = QComboBox()
        self.serial_combo.setEditable(True)
        for device, description in list_serial_ports():
            self.serial_combo.addItem(f"{device} — {description}", device)
        self.serial_row = self._wrap_row("USB port:", self.serial_combo, form)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("10.0.0.9")
        self.host_row = self._wrap_row("Host:", self.host_edit, form)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5555)
        self.port_row = self._wrap_row("Port:", self.port_spin, form)

        self.cutoff_check = QCheckBox("Use meter voltage for discharge cutoff")
        form.addRow(self.cutoff_check)
        layout.addLayout(form)

        conn_row = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.status_label = QLabel("Not connected")
        conn_row.addWidget(self.connect_button)
        conn_row.addWidget(self.status_label, 1)
        layout.addLayout(conn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        layout.addWidget(buttons)

        self.transport_combo.currentIndexChanged.connect(self._update_transport_rows)
        self.connect_button.clicked.connect(self._on_connect_clicked)
        buttons.button(QDialogButtonBox.Save).clicked.connect(self._on_save)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.reject)

        self._load()
        self._update_transport_rows()
        self._refresh_connection_label()

    def _wrap_row(self, label, widget, form):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        form.addRow(label, container)
        container._form_label = form.labelForField(container)
        return container

    def _update_transport_rows(self):
        is_usb = self.transport_combo.currentData() == "usb"
        self._set_row_visible(self.serial_row, is_usb)
        self._set_row_visible(self.host_row, not is_usb)
        self._set_row_visible(self.port_row, not is_usb)

    @staticmethod
    def _set_row_visible(container, visible):
        container.setVisible(visible)
        label = getattr(container, "_form_label", None)
        if label is not None:
            label.setVisible(visible)

    def _load(self):
        s = load_meter_settings(self._settings_file)
        self.enabled_check.setChecked(s.enabled)
        self._select_data(self.profile_combo, s.profile_key)
        self._select_data(self.transport_combo, s.transport)
        if s.serial_port:
            idx = self.serial_combo.findData(s.serial_port)
            if idx >= 0:
                self.serial_combo.setCurrentIndex(idx)
            else:
                self.serial_combo.setEditText(s.serial_port)
        self.host_edit.setText(s.host)
        self.port_spin.setValue(s.lan_port)
        self.cutoff_check.setChecked(s.use_for_cutoff)

    @staticmethod
    def _select_data(combo, value):
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _current_settings(self) -> MeterSettings:
        serial_port = self.serial_combo.currentData()
        if serial_port is None:
            serial_port = self.serial_combo.currentText().split(" — ")[0].strip()
        return MeterSettings(
            enabled=self.enabled_check.isChecked(),
            transport=self.transport_combo.currentData(),
            serial_port=serial_port,
            host=self.host_edit.text().strip(),
            lan_port=self.port_spin.value(),
            profile_key=self.profile_combo.currentData(),
            use_for_cutoff=self.cutoff_check.isChecked(),
        )

    def _on_save(self):
        settings = self._current_settings()
        save_meter_settings(self._settings_file, settings)
        self._on_settings_changed(settings)
        self.accept()

    def _on_connect_clicked(self):
        if self._meter.is_connected:
            self._meter.disconnect()
            self._refresh_connection_label()
            self._on_settings_changed(self._current_settings())
            return
        settings = self._current_settings()
        profile = METER_PROFILES[settings.profile_key]
        try:
            if settings.transport == "usb":
                if not settings.serial_port:
                    QMessageBox.warning(self, "Voltage Monitor", "Choose a USB port.")
                    return
                self._meter.connect_usb(settings.serial_port, profile)
            else:
                if not settings.host:
                    QMessageBox.warning(self, "Voltage Monitor", "Enter the meter host.")
                    return
                self._meter.connect_lan(settings.host, settings.lan_port, profile)
        except MeterError as e:
            QMessageBox.critical(self, "Voltage Monitor", str(e))
            return
        save_meter_settings(self._settings_file, settings)
        self._on_settings_changed(settings)
        self._refresh_connection_label()

    def _refresh_connection_label(self):
        if self._meter.is_connected:
            self.connect_button.setText("Disconnect")
            self.status_label.setText(f"Connected: {self._meter.identity}")
        else:
            self.connect_button.setText("Connect")
            self.status_label.setText("Not connected")
