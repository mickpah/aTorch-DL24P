"""Tests for meter settings load/save in settings.json."""

import json

from load_test_bench.config import (
    MeterSettings,
    load_meter_settings,
    save_meter_settings,
)


class TestMeterSettings:
    def test_defaults_when_file_absent(self, tmp_path):
        s = load_meter_settings(tmp_path / "settings.json")
        assert s == MeterSettings()
        assert s.enabled is False
        assert s.transport == "usb"
        assert s.profile_key == "hds200"

    def test_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        save_meter_settings(
            path,
            MeterSettings(
                enabled=True, transport="lan", host="10.0.0.9", lan_port=5555,
                profile_key="generic_scpi_dmm", use_for_cutoff=True,
            ),
        )
        loaded = load_meter_settings(path)
        assert loaded.enabled is True
        assert loaded.transport == "lan"
        assert loaded.host == "10.0.0.9"
        assert loaded.profile_key == "generic_scpi_dmm"
        assert loaded.use_for_cutoff is True

    def test_save_preserves_other_keys(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"notifications": {"ntfy_enabled": True}}))
        save_meter_settings(path, MeterSettings(enabled=True))
        data = json.loads(path.read_text())
        assert data["notifications"]["ntfy_enabled"] is True
        assert data["meter"]["enabled"] is True

    def test_partial_stored_merges_over_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"meter": {"enabled": True}}))
        loaded = load_meter_settings(path)
        assert loaded.enabled is True
        assert loaded.transport == "usb"  # default preserved

    def test_non_dict_meter_value_returns_defaults(self, tmp_path):
        """A malformed (non-dict) 'meter' value must not crash startup."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"meter": "oops"}))
        assert load_meter_settings(path) == MeterSettings()
