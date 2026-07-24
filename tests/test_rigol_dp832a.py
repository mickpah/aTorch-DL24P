"""Tests for the RigolDP832A LAN driver using a scripted fake socket."""

from load_test_bench.protocol.dp832a_protocol import ChargerStatus
from load_test_bench.protocol.rigol_dp832a import RigolDP832A


class FakeSocket:
    """Stand-in for a TCP socket speaking DP832A SCPI.

    Records every command sent; queues a canned response for queries.
    """

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

    def sendall(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, n):
        data, self._pending = self._pending, b""
        return data

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


class BrokenSocket(FakeSocket):
    """Socket whose writes always fail."""

    def sendall(self, data):
        raise OSError("network unreachable")


def make_device(sock=None):
    """Device wired to a fake socket, bypassing connect() (no poll thread)."""
    device = RigolDP832A()
    device._sock = sock if sock is not None else FakeSocket()
    device._connected = True
    return device


class TestCommands:
    def test_set_voltage_sends_scpi(self):
        device = make_device()
        assert device.set_voltage(4.2) is True
        assert device._sock.sent == [":SOUR1:VOLT 4.200"]

    def test_channel_selection_changes_commands(self):
        """Commands target whichever channel was selected."""
        device = make_device()
        device.set_channel(2)
        device.set_current(1.5)
        assert device._sock.sent == [":SOUR2:CURR 1.500"]

    def test_output_on_off(self):
        device = make_device()
        device.output_on()
        device.output_off()
        assert device._sock.sent == [":OUTP CH1,ON", ":OUTP CH1,OFF"]

    def test_set_ovp_sends_value_then_enable(self):
        device = make_device()
        assert device.set_ovp(4.3) is True
        assert device._sock.sent == [":OUTP:OVP:VAL CH1,4.300", ":OUTP:OVP CH1,ON"]

    def test_command_failure_returns_false_and_reports(self):
        """I/O errors surface via the error callback, never as exceptions."""
        device = make_device(BrokenSocket())
        errors = []
        device.set_error_callback(errors.append)
        assert device.set_voltage(4.2) is False
        assert len(errors) == 1


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
        device = make_device(FakeSocket({":OUTP? CH1": "OFF\n"}))
        status = device._poll_once()
        assert status.output_on is False
        assert status.mode == "UR"
        assert ":OUTP:MODE? CH1" not in device._sock.sent

    def test_poll_tick_clears_last_status_on_failure(self):
        """A failed poll must invalidate last_status - a consumer like
        ChargeMonitor must never mistake stale data for a fresh reading."""
        device = make_device(BrokenSocket())
        device._last_status = ChargerStatus(
            voltage_v=4.1, current_a=0.5, power_w=2.0, output_on=True, mode="CC", channel=1
        )
        device._running = True
        errors = []
        device.set_error_callback(errors.append)
        device._poll_tick()
        assert device.last_status is None
        assert len(errors) == 1

    def test_poll_tick_sets_last_status_on_success(self):
        device = make_device()
        device._running = True
        device._poll_tick()
        status = device.last_status
        assert status is not None
        assert status.voltage_v == 4.105
        assert status.output_on is True
