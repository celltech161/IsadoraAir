"""Raw ASCII TCP client for BW Broadcast TX300v3-family transmitter
control ports.

This is a standalone, additive protocol module -- it has no Django model
dependency and is not wired into monitoring/services/monitor.py or any
other integration point. A common transmitter-driver interface (for
selecting between this, the Aquabroadcast COBALT client, "None", and
future families) is expected to be introduced separately during
integration; this module intentionally does not attempt to guess its
shape.

Recovered and re-verified from an earlier, unmerged local implementation
(originally written against a real TX300 V3 unit), then re-confirmed
live against a real TX300v3 a second time. Protocol notes, differences
from the COBALT dialect, and everything that remains unverified are in
docs/BW_TX300V3_DRIVER_NOTES.md alongside this module.

Quick protocol summary (see the docs file for the full writeup):

    (client)  (connects, TCP)
    (server)  Welcome to the TX-V3!\\r\\npassword:
    (client)  <password>\\r\\n
    (server)  \\r\\r\\n\\r\\nTX-V3>
    (client)  get meters.pafwd\\r\\n
    (server)  259\\r\\n\\r\\nTX-V3>

Responses are bare numbers (or short text) with NO unit suffix and NO
"OK" terminator -- the client has to read until the next "TX-V3>" style
prompt instead. An unsupported parameter name gets an explicit
"Unknown parameter" response rather than an error or a dropped
connection, which this client treats as a distinct condition rather
than a parsed value.
"""
import re
import socket
import time

_IAC, _DONT, _DO, _WONT, _WILL = 255, 254, 253, 252, 251
_NUMERIC_RE = re.compile(r"^(-?\d+(?:\.\d+)?)")
_UNKNOWN_PARAMETER = "unknown parameter"
_INCORRECT_PASSWORD = "incorrect password"

# Parameter names confirmed live against a real TX300v3 (WRJE-LP,
# firmware/unit unspecified -- see docs for the exact verification
# session). Deliberately short: several other plausible names in the
# same style (status.rf, rf.state, meters.freq, status.alarm, ...) were
# tried and got a clean "Unknown parameter" response on this unit, so
# they are NOT included here. Do not add a parameter to this dict on
# guesswork alone -- verify it against a real unit first.
KNOWN_PARAMETERS = {
    "meters.pafwd": "Forward power, watts, bare number (e.g. \"259\").",
    "meters.parev": "Reflected power, watts, bare number (e.g. \"4\").",
    "meters.patemp": (
        "PA temperature, bare number (e.g. \"41\"). Unit is not stated by "
        "the device; assumed degrees Celsius by convention (matches the "
        "equivalent COBALT telemetry and standard broadcast PA reporting) "
        "-- not independently confirmed."
    ),
    "meters.fanspeed": (
        "Fan speed, bare number (e.g. \"16\"). Unit is NOT confirmed -- "
        "could plausibly be a percentage or a PWM duty value rather than "
        "RPM. Exposed as a raw value; do not assume a unit for it."
    ),
    "system.uptime": (
        "Device uptime as a free-text string (e.g. \"20 days, 22:04:36\"), "
        "not a bare number -- do not run it through parse_numeric()."
    ),
}


class BWTx300V3Error(Exception):
    """Base class for all errors raised by this driver."""


class BWTx300V3AuthError(BWTx300V3Error):
    """Raised when the unit rejects the supplied login credentials."""


class BWTx300V3TimeoutError(BWTx300V3Error):
    """Raised when a response doesn't arrive within the configured timeout."""


class BWTx300V3ProtocolError(BWTx300V3Error):
    """Raised for a well-formed but unusable response, e.g. querying a
    parameter name the unit doesn't recognize."""


class BWTx300V3Client:
    """Client for a BW TX300v3-family transmitter's ASCII control port.

    Confirmed live: this unit's login is password-only -- it never asks
    for a username at all. The username-then-password path below is
    kept because the earlier local implementation's docstring noted
    "some BW units DO ask for username first" -- that claim was never
    independently re-verified this session (WRJE's own unit doesn't use
    it), so treat that branch as unconfirmed-but-preserved rather than
    freshly verified.

    No default password is provided anywhere in this class -- BW units
    are commonly left on a factory-default credential, but hardcoding
    any specific value here would bake a real, working station
    credential into shared source. Callers must always supply one.

    Interface (`__enter__`/`__exit__`/`get`) intentionally mirrors the
    unrelated COBALT client in transmitter_client.py so a future common
    interface can wrap either with the same call shape -- this module
    does not attempt to define that shared interface itself.
    """

    def __init__(self, host, port=23, timeout=3.0, password=None, username=None):
        if not password:
            raise ValueError(
                "password is required -- this driver does not ship a "
                "default credential."
            )
        self.host = host
        self.port = port
        self.timeout = timeout
        self.password = password
        # Only used if the unit's banner prompts for a username first;
        # WRJE's own unit never does. See class docstring.
        self.username = username or "admin"
        self._sock = None

    def __enter__(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._login()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        return False

    def _strip_iac(self, data):
        """Remove telnet IAC option-negotiation sequences, replying
        WONT/DONT to any WILL/DO requests -- same approach as the
        COBALT client, needed here too since this unit also does a
        real RFC854 negotiation on connect."""
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

    def _read_until_any(self, markers):
        # Every exit path below -- socket timeout, the remote closing
        # the connection early, or the wall-clock deadline expiring --
        # falls through to the same marker check at the end, rather
        # than trusting a particular break reason to mean "done". A
        # closed connection mid-response looks identical to a slow one
        # here: neither is a valid response, so both raise the same way.
        buf = b""
        end = time.time() + self.timeout
        while time.time() < end:
            try:
                chunk = self._sock.recv(2048)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += self._strip_iac(chunk)
            text = buf.decode(errors="replace")
            if any(m in text for m in markers):
                return text
        raise BWTx300V3TimeoutError(
            f"Timed out waiting for one of {markers!r} from the transmitter."
        )

    def _login(self):
        # Captured live: a rejected password gets "...Incorrect password."
        # with NO trailing ">" prompt at all -- watching for ">" alone
        # would just time out on a wrong password instead of reporting
        # it. Watch for both terminal shapes.
        banner = self._read_until_any([":", ">"])
        if "password" in banner.lower():
            self._sock.sendall((self.password + "\r\n").encode())
            resp = self._read_until_any([">", "Incorrect"])
        else:
            self._sock.sendall((self.username + "\r\n").encode())
            self._read_until_any([":", ">"])
            self._sock.sendall((self.password + "\r\n").encode())
            resp = self._read_until_any([">", "Incorrect"])
        if _INCORRECT_PASSWORD in resp.lower():
            raise BWTx300V3AuthError("TX300v3 rejected the supplied credentials.")

    def get(self, parameter):
        """Send `get <parameter>` and return the raw string value.

        Raises BWTx300V3ProtocolError if the unit responds with its
        "Unknown parameter" text rather than a value -- callers should
        only pass names from KNOWN_PARAMETERS (or a name they have
        independently verified against a real unit)."""
        self._sock.sendall(f"get {parameter}\r\n".encode())
        raw = self._read_until_any([">"])
        lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
        lines = [ln for ln in lines if parameter not in ln]
        value = None
        for ln in reversed(lines):
            candidate = re.sub(r"^\S*>\s*", "", ln).strip()
            if candidate:
                value = candidate
                break
        if value is not None and value.lower() == _UNKNOWN_PARAMETER:
            raise BWTx300V3ProtocolError(f"Unit does not recognize parameter {parameter!r}.")
        return value

    def read_status(self):
        """Convenience read of every parameter this driver has actually
        verified against a real unit, using canonical field names.

        Only includes fields backed by a parameter in KNOWN_PARAMETERS.
        Notably absent: RF on/off state, operating frequency, and any
        alarm/fault list -- plausible parameter names for all three were
        tried live against a real TX300v3 and got a clean "Unknown
        parameter" response, so none are included here. See
        docs/BW_TX300V3_DRIVER_NOTES.md for exactly what was tried."""
        fwd = parse_numeric(self.get("meters.pafwd"))
        rev = parse_numeric(self.get("meters.parev"))
        pa_temp = parse_numeric(self.get("meters.patemp"))
        fan_raw = parse_numeric(self.get("meters.fanspeed"))
        uptime_raw = self.get("system.uptime")
        return {
            "forward_power_watts": fwd,
            "reflected_power_watts": rev,
            "vswr": compute_vswr(fwd, rev),
            "pa_temperature_c": pa_temp,
            "fan_speed_raw": fan_raw,
            "uptime_raw": uptime_raw,
        }


def parse_numeric(raw):
    """Unlike the COBALT client's parse_numeric, TX300v3 `get` responses
    are bare numbers with no unit suffix -- but reuse the same
    leading-numeric-match approach rather than requiring the whole
    string to parse as a float, since it's cheap insurance against
    incidental trailing whitespace/noise."""
    if raw is None:
        return None
    match = _NUMERIC_RE.match(raw.strip())
    return float(match.group(1)) if match else None


def compute_vswr(forward_watts, reflected_watts):
    """Same derivation as the COBALT integration's client-side VSWR
    calc (this unit doesn't expose VSWR as its own parameter either --
    only meters.pafwd/meters.parev were found to work). Returns None
    when the inputs can't produce a meaningful ratio, or when reflected
    power is at or above forward power (a fault condition, not a huge-
    but-valid VSWR) -- callers should treat None from this specific
    fault case as "critical", not "unknown"; distinguishing the two is
    left to the integration layer since it depends on how that layer
    represents status."""
    if forward_watts is None or reflected_watts is None or forward_watts <= 0:
        return None
    ratio = reflected_watts / forward_watts
    if ratio >= 1:
        return None
    rho = ratio ** 0.5
    return (1 + rho) / (1 - rho)
