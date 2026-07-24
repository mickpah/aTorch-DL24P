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

    def test_disconnect_drops_the_transport(self):
        """After disconnect a stale, host-bound transport must never be
        reused - the next connect() has to build a fresh one, so a changed
        host cannot silently dial the old instrument."""
        device = make_device()
        device.disconnect()
        assert device._transport is None
        assert device.host is None
        assert device.output_off() is False  # no transport -> command dropped


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
