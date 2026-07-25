"""Tests for the SCPI meter profiles and measurement parsing."""

import pytest

from load_test_bench.protocol.meter_protocol import (
    METER_PROFILES,
    make_idn_verifier,
    parse_measurement,
)


class TestParseMeasurement:
    def test_plain_float(self):
        assert parse_measurement("4.187\n") == 4.187

    def test_scientific_notation(self):
        assert parse_measurement("4.187000e+00\n") == pytest.approx(4.187)

    def test_strips_units_and_whitespace(self):
        assert parse_measurement("  4.187 V \r\n") == 4.187

    def test_negative(self):
        assert parse_measurement("-0.512") == -0.512

    def test_no_number_raises(self):
        with pytest.raises(ValueError):
            parse_measurement("OVERLOAD")


class TestProfiles:
    def test_hds200_profile(self):
        """OWON HDS200 uses its DMM subsystem over USB."""
        p = METER_PROFILES["hds200"]
        assert p.default_transport == "usb"
        assert p.measure_command == ":DMM:MEAS?"
        assert ":DMM:CONFigure:VOLTage DC" in p.setup_commands
        assert ":DMM:AUTO ON" in p.setup_commands

    def test_generic_profile_uses_standard_scpi(self):
        """A standard bench DMM uses MEASure:VOLTage:DC? and no setup."""
        p = METER_PROFILES["generic_scpi_dmm"]
        assert p.measure_command == ":MEASure:VOLTage:DC?"
        assert p.setup_commands == ()
        assert p.default_transport == "lan"

    def test_all_profiles_have_unique_keys_matching_dict(self):
        for key, profile in METER_PROFILES.items():
            assert profile.key == key


class TestIdnVerifier:
    def test_matches_substring_case_insensitive(self):
        verify = make_idn_verifier(METER_PROFILES["hds200"])
        assert verify("owon,hds242,2128009,V2.1.1.5") is True

    def test_rejects_non_matching_when_constrained(self):
        verify = make_idn_verifier(METER_PROFILES["hds200"])
        assert verify("RIGOL TECHNOLOGIES,DP832A,X,Y") is False

    def test_empty_constraint_accepts_any_responding_instrument(self):
        verify = make_idn_verifier(METER_PROFILES["generic_scpi_dmm"])
        assert verify("ANYTHING,MODEL,SN,VER") is True
