"""Tests for the USB (pyserial CDC) SCPI link."""

import pytest

from load_test_bench.protocol.scpi_transport import ScpiTransport, UsbScpiLink


class FakeSerial:
    """Stand-in for a pyserial Serial: buffers a scripted reply."""

    def __init__(self, reply: bytes = b""):
        self._rx = reply
        self.written = b""
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def write(self, data):
        self.written += data

    def read(self, n):
        chunk, self._rx = self._rx[:n], self._rx[n:]
        return chunk

    def close(self):
        self.closed = True


class TestUsbScpiLink:
    def test_send_writes_to_serial(self):
        fake = FakeSerial()
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        link.open()  # no-op with injected serial
        link.send(b"*IDN?\n")
        assert fake.written == b"*IDN?\n"

    def test_recv_returns_buffered_bytes(self):
        fake = FakeSerial(reply=b"4.187\n")
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        assert link.recv(4096) == b"4.187\n"

    def test_recv_timeout_raises_oserror(self):
        """No data buffered and a blocking read returning empty is a timeout."""
        fake = FakeSerial(reply=b"")
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        with pytest.raises(OSError):
            link.recv(4096)

    def test_close_closes_serial(self):
        fake = FakeSerial()
        link = UsbScpiLink("/dev/ttyUSB0", serial_obj=fake)
        link.close()
        assert fake.closed is True
        assert link._serial is None

    def test_send_before_open_raises(self):
        link = UsbScpiLink("/dev/ttyUSB0")
        with pytest.raises(OSError):
            link.send(b"x\n")


class TestTransportOverUsb:
    def test_query_round_trip_through_transport(self):
        """A full ScpiTransport query works over the USB link (line-framed)."""
        fake = FakeSerial(reply=b"OWON,HDS242,2128009,V2.1.1.5\n")
        transport = ScpiTransport(UsbScpiLink("/dev/ttyUSB0", serial_obj=fake))
        transport.connect(lambda idn: "OWON" in idn, describe="meter")
        assert transport.is_connected is True
        assert "OWON" in transport.identity
