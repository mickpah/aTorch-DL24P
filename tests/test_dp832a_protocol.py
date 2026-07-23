"""Tests for the Rigol DP832A SCPI protocol builder/parser."""

import pytest

from load_test_bench.protocol.dp832a_protocol import (
    CHANNEL_LIMITS,
    ChargerStatus,
    DP832AProtocol,
)


class TestCommandBuilding:
    def test_idn_query(self):
        """Identification query is the bare SCPI standard command."""
        assert DP832AProtocol.cmd_idn() == "*IDN?"

    def test_set_voltage_ch1(self):
        """Voltage setpoint uses the SOUR<n> form with 3 decimals."""
        assert DP832AProtocol.cmd_set_voltage(1, 4.2) == ":SOUR1:VOLT 4.200"

    def test_set_voltage_rejects_over_range(self):
        """CH3 tops out at 5.3 V - higher setpoints are refused."""
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_voltage(3, 12.0)

    def test_set_voltage_rejects_bad_channel(self):
        """The DP832A only has channels 1-3."""
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_voltage(4, 1.0)

    def test_set_current(self):
        assert DP832AProtocol.cmd_set_current(2, 0.5) == ":SOUR2:CURR 0.500"

    def test_set_current_rejects_over_range(self):
        with pytest.raises(ValueError):
            DP832AProtocol.cmd_set_current(1, 5.0)

    def test_output_on_off(self):
        assert DP832AProtocol.cmd_set_output(1, True) == ":OUTP CH1,ON"
        assert DP832AProtocol.cmd_set_output(3, False) == ":OUTP CH3,OFF"

    def test_queries(self):
        assert DP832AProtocol.cmd_query_output(1) == ":OUTP? CH1"
        assert DP832AProtocol.cmd_measure_all(2) == ":MEAS:ALL? CH2"
        assert DP832AProtocol.cmd_query_mode(1) == ":OUTP:MODE? CH1"

    def test_ovp_commands(self):
        assert DP832AProtocol.cmd_set_ovp_value(1, 4.3) == ":OUTP:OVP:VAL CH1,4.300"
        assert DP832AProtocol.cmd_set_ovp_state(1, True) == ":OUTP:OVP CH1,ON"

    def test_channel_limits_cover_all_channels(self):
        """CH1/CH2 are the 30 V channels, CH3 the 5 V channel."""
        assert CHANNEL_LIMITS[1] == (32.0, 3.2)
        assert CHANNEL_LIMITS[2] == (32.0, 3.2)
        assert CHANNEL_LIMITS[3] == (5.3, 3.2)


class TestResponseParsing:
    def test_parse_idn_accepts_dp832(self):
        """Real instrument IDN string (with trailing newline) is accepted."""
        idn = "RIGOL TECHNOLOGIES,DP832A,DP8A123456789,00.01.16\n"
        assert DP832AProtocol.parse_idn(idn) is True

    def test_parse_idn_rejects_other_instruments(self):
        assert DP832AProtocol.parse_idn("RIGOL TECHNOLOGIES,DS1054Z,X,Y") is False
        assert DP832AProtocol.parse_idn("garbage") is False

    def test_parse_measure_all(self):
        """':MEAS:ALL?' returns 'volts,amps,watts'."""
        assert DP832AProtocol.parse_measure_all("4.105,0.512,2.102\n") == (
            4.105,
            0.512,
            2.102,
        )

    def test_parse_measure_all_rejects_garbage(self):
        with pytest.raises(ValueError):
            DP832AProtocol.parse_measure_all("ERR")

    def test_parse_output_state(self):
        assert DP832AProtocol.parse_output_state("ON\n") is True
        assert DP832AProtocol.parse_output_state("OFF\n") is False

    def test_parse_mode(self):
        assert DP832AProtocol.parse_mode("CV\n") == "CV"
        assert DP832AProtocol.parse_mode("CC\n") == "CC"
        with pytest.raises(ValueError):
            DP832AProtocol.parse_mode("??")


class TestChargerStatus:
    def test_fields(self):
        """ChargerStatus carries one channel's live snapshot."""
        status = ChargerStatus(
            voltage_v=4.1,
            current_a=0.5,
            power_w=2.05,
            output_on=True,
            mode="CC",
            channel=1,
        )
        assert status.voltage_v == 4.1
        assert status.output_on is True
        assert status.channel == 1
