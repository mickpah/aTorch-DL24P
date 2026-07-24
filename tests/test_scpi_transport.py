"""Tests for the link-agnostic SCPI transport."""

import pytest

from load_test_bench.protocol.scpi_transport import (
    LanScpiLink,
    ScpiError,
    ScpiTransport,
)


class FakeLink:
    """Scripted ScpiLink: records sent commands, replies from a table."""

    def __init__(self, responses=None):
        self.responses = {"*IDN?": "RIGOL TECHNOLOGIES,DP832A,DP8A1,00.01.16\n"}
        if responses:
            self.responses.update(responses)
        self.sent = []
        self._pending = b""
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def send(self, data):
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        if cmd in self.responses:
            self._pending = self.responses[cmd].encode("ascii")

    def recv(self, max_bytes):
        data, self._pending = self._pending, b""
        return data


class BrokenLink(FakeLink):
    def send(self, data):
        raise OSError("unreachable")


class UnreachableLink(FakeLink):
    def open(self):
        raise OSError("no route to host")


class TestConnect:
    def test_connect_verifies_identity(self):
        link = FakeLink()
        transport = ScpiTransport(link)
        transport.connect(lambda idn: "DP832A" in idn, describe="test instrument")
        assert transport.is_connected is True
        assert "DP832A" in transport.identity

    def test_connect_rejects_wrong_instrument(self):
        link = FakeLink({"*IDN?": "RIGOL TECHNOLOGIES,DS1054Z,X,Y\n"})
        transport = ScpiTransport(link)
        with pytest.raises(ScpiError):
            transport.connect(lambda idn: "DP832A" in idn, describe="test instrument")
        assert link.closed is True

    def test_connect_unreachable_raises(self):
        transport = ScpiTransport(UnreachableLink())
        with pytest.raises(ScpiError):
            transport.connect(lambda idn: True, describe="test instrument")


class TestCommands:
    def make_connected(self, link=None):
        transport = ScpiTransport(link if link is not None else FakeLink())
        transport._connected = True
        return transport

    def test_command_writes_terminated_line(self):
        transport = self.make_connected()
        assert transport.command(":OUTP CH1,ON") is True
        assert transport._link.sent == [":OUTP CH1,ON"]

    def test_command_failure_reports_and_returns_false(self):
        transport = self.make_connected(BrokenLink())
        errors = []
        transport.set_error_callback(errors.append)
        assert transport.command(":OUTP CH1,OFF") is False
        assert len(errors) == 1

    def test_command_lock_busy_drops_with_error(self):
        """The GUI lock-timeout path: a held lock drops the command."""
        transport = self.make_connected()
        transport._lock_timeout = 0.01
        errors = []
        transport.set_error_callback(errors.append)
        assert transport._lock.acquire()
        try:
            assert transport.command(":OUTP CH1,OFF") is False
        finally:
            transport._lock.release()
        assert len(errors) == 1
        assert transport._link.sent == []


class TestPolling:
    def test_poll_tick_stores_status(self):
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        transport._poll_fn = lambda: {"voltage": 4.1}
        transport._poll_tick()
        assert transport.last_status == {"voltage": 4.1}

    def test_poll_tick_clears_last_status_on_failure(self):
        """A failed poll must invalidate last_status - consumers must never
        mistake stale data for a fresh reading."""
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        transport._last_status = {"voltage": 4.1}
        errors = []
        transport.set_error_callback(errors.append)

        def failing_poll():
            raise OSError("gone")

        transport._poll_fn = failing_poll
        transport._poll_tick()
        assert transport.last_status is None
        assert len(errors) == 1

    def test_status_callback_fires_on_success(self):
        transport = ScpiTransport(FakeLink())
        transport._connected = True
        transport._running = True
        seen = []
        transport.set_status_callback(seen.append)
        transport._poll_fn = lambda: "status"
        transport._poll_tick()
        assert seen == ["status"]


class TestLanScpiLink:
    def test_injected_socket_used_verbatim(self):
        class Sock:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def recv(self, n):
                return b"ok\n"

            def close(self):
                pass

            def settimeout(self, t):
                pass

        sock = Sock()
        link = LanScpiLink("h", 5555, sock=sock)
        link.open()  # no-op with injected socket
        link.send(b"*IDN?\n")
        assert sock.sent == b"*IDN?\n"
        assert link.recv(64) == b"ok\n"
