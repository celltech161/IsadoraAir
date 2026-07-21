"""Raw ASCII TCP client for the Aquabroadcast COBALT transmitter's
control protocol (see the Cobalt user manual, pages 62-63 in the
v1.16 revision) -- NOT telnetlib (removed entirely in Python 3.14
anyway).

Verified live against the real unit at KOGR-LP that this is NOT
"plain ASCII, no real telnet" as the manual's command list alone
suggested -- the device sends a genuine RFC854 IAC negotiation
(`\\xff\\xfd\\x18`, i.e. IAC DO TERMINAL-TYPE) immediately on connect,
and drops into an interactive `Cobalt>` prompt after every response.
Concretely, one full command/response cycle looks like:

    (client)  get psu.fwd_power\\r\\n
    (server)  get psu.fwd_power\\r\\n249.42 W\\r\\nOK\\r\\n\\r\\nCobalt>

-- the command echoed back, then the value (WITH a unit suffix --
"249.42 W", "45.87 degC", "5182 RPM" -- not a bare number), then "OK",
then a trailing "Cobalt>" prompt with NO newline after it. That trailing
prompt has to be actively drained before the next command is sent, or
its bytes silently prefix the next response and corrupt the parse --
this was caught live (first attempt produced "Cobalt>get psu.fwd_power"
as a single garbled line) before being fixed here."""
import re
import socket
import time

_IAC, _DONT, _DO, _WONT, _WILL = 255, 254, 253, 252, 251
_UNIT_SUFFIX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)")


class TransmitterError(Exception):
    pass


class TransmitterClient:
    def __init__(self, host, port, timeout=3.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def __enter__(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._buf = b""
        self._drain_negotiation()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._sock:
            try:
                self._sock.sendall(b"quit\r\n")
            except OSError:
                pass
            self._sock.close()
        return False

    def _strip_iac(self, data):
        """Remove telnet IAC option-negotiation sequences, replying
        WONT/DONT to any WILL/DO requests so the server stops waiting on
        an option this bare-bones client doesn't actually support."""
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == _IAC and i + 2 < len(data):
                cmd, opt = data[i + 1], data[i + 2]
                if cmd == _DO:
                    self._sock.sendall(bytes([_IAC, _WONT, opt]))
                elif cmd == _WILL:
                    self._sock.sendall(bytes([_IAC, _DONT, opt]))
                i += 3
                continue
            out.append(b)
            i += 1
        return bytes(out)

    def _drain_negotiation(self):
        # Real bug hit live: recv()'s own blocking timeout is
        # self.timeout (e.g. 5s), which is much longer than the ~1.5s
        # window this is meant to spend -- if nothing more arrives after
        # the IAC negotiation, a plain recv() call blocks for the FULL
        # 5s instead of returning quickly. Use a short per-call socket
        # timeout for the drain itself, then restore the real one.
        self._sock.settimeout(0.2)
        try:
            end = time.time() + min(1.5, self.timeout)
            while time.time() < end:
                try:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    self._strip_iac(chunk)  # just need any IAC replies sent
                except socket.timeout:
                    break
        finally:
            self._sock.settimeout(self.timeout)
        self._buf = b""  # discard the initial banner/prompt noise, not part of any command's response

    def _read_until(self, marker):
        end = time.time() + self.timeout
        while marker not in self._buf and time.time() < end:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise TransmitterError("Transmitter closed the connection unexpectedly.")
                self._buf += self._strip_iac(chunk)
            except socket.timeout:
                continue
        if marker not in self._buf:
            raise TransmitterError(f"Timed out waiting for {marker!r} from the transmitter.")
        idx = self._buf.index(marker) + len(marker)
        result, self._buf = self._buf[:idx], self._buf[idx:]
        return result

    def _drain_prompt(self):
        # The trailing "Cobalt>" prompt has no newline after it -- if
        # left in the buffer it silently prefixes the next command's
        # response. Discard whatever's already buffered, then mop up
        # anything still trickling in off the wire, and discard that too.
        #
        # Real bug hit live: with the socket's real timeout (e.g. 5s)
        # still in effect, this "quick 0.3s drain" instead blocked for
        # the FULL 5s on every single command (9 transmitter checks/poll
        # -> ~45s of dead time once a minute), which was the actual
        # cause of the monitoring dashboard's stale-data banner flicker.
        # Use a short per-call socket timeout for the drain, then
        # restore the real one.
        self._buf = b""
        self._sock.settimeout(0.2)
        try:
            end = time.time() + 0.3
            while time.time() < end:
                try:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    self._strip_iac(chunk)
                except socket.timeout:
                    break
        finally:
            self._sock.settimeout(self.timeout)
        self._buf = b""

    def _send_command(self, command):
        self._sock.sendall((command + "\r\n").encode("ascii"))
        raw = self._read_until(b"OK\r\n")
        self._drain_prompt()
        text = raw.decode("ascii", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # First line is the echoed command; drop it and the trailing "OK".
        body = [line for line in lines if line != command and line != "OK"]
        return body[0] if body else None

    def ping(self):
        response = self._send_command("ping")
        return response is not None and response.strip().lower() == "pong"

    def get(self, parameter):
        return self._send_command(f"get {parameter}")


def parse_numeric(raw):
    """Get responses carry a unit suffix (e.g. "249.42 W", "45.87 degC",
    "5182 RPM") rather than a bare number -- pull the leading numeric
    portion out rather than requiring the whole string to parse as a float."""
    if raw is None:
        return None
    match = _UNIT_SUFFIX_RE.match(raw.strip())
    return float(match.group(1)) if match else None
