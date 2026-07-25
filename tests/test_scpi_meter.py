"""Tests for the ScpiMeter driver over a scripted fake link."""

from load_test_bench.jobs.devices import MeterDevice, MeterStatus
from load_test_bench.protocol.meter_protocol import METER_PROFILES
from load_test_bench.protocol.scpi_meter import ScpiMeter
from load_test_bench.protocol.scpi_transport import ScpiTransport


class FakeLink:
    """Scripted ScpiLink for a DMM: *IDN? plus a measure reply."""

    def __init__(self, responses=None):
        self.responses = {
            "*IDN?": "OWON,HDS242,2128009,V2.1.1.5\n",
            ":DMM:MEAS?": "4.187\n",
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


def make_meter(link=None):
    """Meter wired to a fake link's transport, bypassing connect_*/polling."""
    transport = ScpiTransport(link if link is not None else FakeLink())
    transport._connected = True
    meter = ScpiMeter(transport=transport)
    meter._profile = METER_PROFILES["hds200"]
    return meter


class TestConformance:
    def test_scpi_meter_is_a_meter_device(self):
        assert isinstance(ScpiMeter(), MeterDevice)


class TestPolling:
    def test_poll_once_parses_voltage(self):
        meter = make_meter()
        status = meter._poll_once()
        assert isinstance(status, MeterStatus)
        assert status.voltage_v == 4.187
        assert meter._transport._link.sent == [":DMM:MEAS?"]

    def test_generic_profile_uses_standard_measure(self):
        link = FakeLink({":MEASure:VOLTage:DC?": "3.702\n"})
        transport = ScpiTransport(link)
        transport._connected = True
        meter = ScpiMeter(transport=transport)
        meter._profile = METER_PROFILES["generic_scpi_dmm"]
        status = meter._poll_once()
        assert status.voltage_v == 3.702
        assert link.sent == [":MEASure:VOLTage:DC?"]


class TestConnect:
    def test_connect_runs_setup_then_polls(self):
        """connect verifies IDN, issues setup commands, then starts polling."""
        link = FakeLink()
        meter = ScpiMeter()
        assert meter.connect_lan("10.0.0.9", 5555, METER_PROFILES["hds200"],
                                 _link=link) is True
        # setup commands were sent after the *IDN? handshake
        assert ":DMM:CONFigure:VOLTage DC" in link.sent
        assert ":DMM:AUTO ON" in link.sent
        assert meter.is_connected is True
        meter.disconnect()

    def test_connect_rejects_wrong_instrument(self):
        import pytest

        from load_test_bench.protocol.scpi_meter import MeterError

        link = FakeLink({"*IDN?": "RIGOL TECHNOLOGIES,DP832A,X,Y\n"})
        meter = ScpiMeter()
        with pytest.raises(MeterError):
            meter.connect_lan("10.0.0.9", 5555, METER_PROFILES["hds200"], _link=link)

    def test_commands_without_transport_are_safe(self):
        """A never-connected meter reports no status rather than raising."""
        meter = ScpiMeter()
        assert meter.last_status is None
        assert meter.is_connected is False
