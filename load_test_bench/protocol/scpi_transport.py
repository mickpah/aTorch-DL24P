"""Generic SCPI instrument transport, link-agnostic.

Two layers (see docs/superpowers/specs/2026-07-24-job-engine-design.md):
ScpiLink is a minimal byte pipe - LAN socket today (LanScpiLink), USB CDC
serial later when the HDS200 meter driver lands. ScpiTransport adds line
framing, *IDN? verification, the lock-timeout command pattern (CLAUDE.md
"Lock Timeout for GUI Operations"), and a poll thread that invalidates
last_status on failure so stale data can never be mistaken for fresh.

Device drivers (RigolDP832A, future instruments) supply only SCPI string
building/parsing plus a poll_once() that reads their status under the
transport lock. Status/error callbacks fire on the poll thread - GUI
consumers must marshal through Qt Signals.
"""

import socket
import threading
import time
from typing import Callable, Optional, Protocol


class ScpiError(Exception):
    """Raised on SCPI connection or identification failures."""


class ScpiLink(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, data: bytes) -> None: ...
    def recv(self, max_bytes: int) -> bytes: ...


class LanScpiLink:
    """SCPI over a TCP socket (e.g. Rigol DP832A, raw SCPI on port 5555).

    A pre-opened socket-like object may be injected for tests; open() is then
    a no-op.
    """

    def __init__(self, host: str, port: int, timeout: float = 2.0, sock=None) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock = sock

    def open(self) -> None:
        if self._sock is None:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
            self._sock.settimeout(self._timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise OSError("Link not open")
        self._sock.sendall(data)

    def recv(self, max_bytes: int) -> bytes:
        if self._sock is None:
            raise OSError("Link not open")
        return self._sock.recv(max_bytes)


class ScpiTransport:
    POLL_INTERVAL = 1.0  # seconds
    LOCK_TIMEOUT = 1.0  # seconds; GUI commands must never block longer

    def __init__(
        self,
        link: ScpiLink,
        poll_interval: float = POLL_INTERVAL,
        lock_timeout: float = LOCK_TIMEOUT,
    ) -> None:
        self._link = link
        self._poll_interval = poll_interval
        self._lock_timeout = lock_timeout
        self._lock = threading.Lock()
        self._connected = False
        self._identity = ""
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_fn: Optional[Callable[[], object]] = None
        self._last_status: Optional[object] = None
        self._status_callback: Optional[Callable[[object], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def last_status(self) -> Optional[object]:
        return self._last_status

    def set_status_callback(self, callback: Callable[[object], None]) -> None:
        self._status_callback = callback

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback

    def connect(self, verify_idn: Callable[[str], bool], describe: str = "instrument") -> None:
        if self._connected:
            return
        try:
            self._link.open()
        except OSError as e:
            raise ScpiError(f"Cannot reach {describe}: {e}") from e
        try:
            idn = self.query("*IDN?")
        except OSError as e:
            self._link.close()
            raise ScpiError(f"No SCPI response from {describe}: {e}") from e
        if not verify_idn(idn):
            self._link.close()
            raise ScpiError(f"Unexpected instrument at {describe}: {idn!r}")
        self._identity = idn.strip()
        self._connected = True

    def start_polling(self, poll_once: Callable[[], object]) -> None:
        self._poll_fn = poll_once
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=self._poll_interval + self._lock_timeout + 2.0)
        self._poll_thread = None
        self._link.close()
        self._connected = False
        self._last_status = None

    def command(self, cmd: str) -> bool:
        """Fire a set-command with the GUI lock timeout. Never raises on I/O."""
        if not self._lock.acquire(timeout=self._lock_timeout):
            self._report_error(f"Instrument busy, command dropped: {cmd}")
            return False
        try:
            self.write(cmd)
            return True
        except OSError as e:
            self._report_error(f"Instrument command failed: {e}")
            return False
        finally:
            self._lock.release()

    def run_locked(self, fn: Callable[[], object]) -> object:
        with self._lock:
            return fn()

    def write(self, cmd: str) -> None:
        self._link.send((cmd + "\n").encode("ascii"))

    def read_line(self) -> str:
        chunks = []
        while True:
            data = self._link.recv(4096)
            if not data:
                raise OSError("Connection closed by instrument")
            chunks.append(data)
            if data.endswith(b"\n"):
                break
        return b"".join(chunks).decode("ascii").strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read_line()

    def _poll_tick(self) -> None:
        try:
            status = self._poll_fn()
            self._last_status = status
            if self._status_callback:
                try:
                    self._status_callback(status)
                except Exception:
                    pass
        except Exception as e:
            self._last_status = None
            if self._running:
                self._report_error(f"Instrument poll failed: {e}")

    def _poll_loop(self) -> None:
        while self._running:
            start = time.monotonic()
            self._poll_tick()
            remaining = self._poll_interval - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)

    def _report_error(self, message: str) -> None:
        if self._error_callback:
            try:
                self._error_callback(message)
            except Exception:
                pass
