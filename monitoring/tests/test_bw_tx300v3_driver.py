"""monitoring/services/transmitter_drivers/bw_tx300v3.py tests.

No live transmitter dependency -- socket.create_connection is patched
to hand back a FakeSocket driven by sanitized, captured wire-format
fixtures (real response shapes observed live against a TX300v3,
credential values replaced with test-only placeholders that never
touch this file)."""
from unittest.mock import patch

from django.test import SimpleTestCase

from monitoring.services.transmitter_drivers.bw_tx300v3 import (
    BWTx300V3AuthError,
    BWTx300V3Client,
    BWTx300V3ProtocolError,
    BWTx300V3TimeoutError,
    compute_vswr,
    parse_numeric,
)

# Captured live, sanitized: no real host/credential value appears in any
# fixture below -- only the device's own response text, which contains
# none.
BANNER_PASSWORD_ONLY = b"Welcome to the TX-V3!\r\npassword: "
POST_LOGIN_PROMPT = b"\r\r\n\r\nTX-V3> "
LOGIN_REJECTED = b"\r\n\r\nIncorrect password."


def _get_response(parameter, value):
    return f"get {parameter}\r\n{value}\r\n\r\nTX-V3> ".encode()


class FakeSocket:
    """Stateless replay of pre-scripted recv() chunks -- sendall() is
    recorded but never inspected to pick a response, since each test
    already knows the exact command sequence it's driving."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _bufsize):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        pass


def _client_with_socket(fake_sock, **kwargs):
    kwargs.setdefault("password", "test-only-placeholder")
    client = BWTx300V3Client("203.0.113.10", **kwargs)
    with patch("socket.create_connection", return_value=fake_sock):
        client.__enter__()
    return client


class BWTx300V3ClientTests(SimpleTestCase):
    def test_requires_a_password(self):
        with self.assertRaises(ValueError):
            BWTx300V3Client("203.0.113.10", password=None)
        with self.assertRaises(ValueError):
            BWTx300V3Client("203.0.113.10", password="")

    def test_password_only_login_matches_real_unit(self):
        """WRJE's real TX300v3 skips the username prompt entirely --
        this is the exact banner/response shape confirmed live."""
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        # Only one line sent: the password. No username line at all.
        self.assertEqual(sock.sent, [b"test-only-placeholder\r\n"])
        client.__exit__(None, None, None)

    def test_username_then_password_login_fallback_path(self):
        """Preserved from the earlier implementation's docstring claim
        that some BW units ask for a username first -- not re-verified
        against WRJE's own unit this session, but kept as a documented,
        exercised fallback rather than dropped silently."""
        banner = b"Welcome to the TX-V3!\r\nusername: "
        sock = FakeSocket([banner, b"password: ", POST_LOGIN_PROMPT])
        client = _client_with_socket(sock, username="operator")
        self.assertEqual(sock.sent, [b"operator\r\n", b"test-only-placeholder\r\n"])
        client.__exit__(None, None, None)

    def test_incorrect_password_raises_auth_error(self):
        sock = FakeSocket([BANNER_PASSWORD_ONLY, LOGIN_REJECTED])
        with self.assertRaises(BWTx300V3AuthError):
            _client_with_socket(sock)

    def test_get_returns_bare_number_value(self):
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        sock._chunks.append(_get_response("meters.pafwd", "259"))
        self.assertEqual(client.get("meters.pafwd"), "259")
        client.__exit__(None, None, None)

    def test_get_value_split_across_multiple_recv_calls(self):
        """The value can arrive split mid-line across TCP segments --
        confirm buffering across recv() calls works, not just the
        single-chunk happy path."""
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        full = _get_response("meters.parev", "4")
        split_at = len(full) // 2
        sock._chunks.extend([full[:split_at], full[split_at:]])
        self.assertEqual(client.get("meters.parev"), "4")
        client.__exit__(None, None, None)

    def test_get_unsupported_parameter_raises_protocol_error(self):
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        sock._chunks.append(_get_response("status.rf", "Unknown parameter"))
        with self.assertRaises(BWTx300V3ProtocolError):
            client.get("status.rf")
        client.__exit__(None, None, None)

    def test_get_times_out_when_prompt_never_arrives(self):
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        client.timeout = 0.05  # keep the test fast
        sock._chunks.append(b"get meters.pafwd\r\n259\r\n")  # no trailing prompt
        with self.assertRaises(BWTx300V3TimeoutError):
            client.get("meters.pafwd")
        client.__exit__(None, None, None)

    def test_read_status_returns_only_verified_fields(self):
        sock = FakeSocket([BANNER_PASSWORD_ONLY, POST_LOGIN_PROMPT])
        client = _client_with_socket(sock)
        sock._chunks.extend([
            _get_response("meters.pafwd", "259"),
            _get_response("meters.parev", "4"),
            _get_response("meters.patemp", "41"),
            _get_response("meters.fanspeed", "16"),
            _get_response("system.uptime", "20 days, 22:04:36"),
        ])
        status = client.read_status()
        self.assertEqual(status["forward_power_watts"], 259.0)
        self.assertEqual(status["reflected_power_watts"], 4.0)
        self.assertEqual(status["pa_temperature_c"], 41.0)
        self.assertEqual(status["fan_speed_raw"], 16.0)
        self.assertEqual(status["uptime_raw"], "20 days, 22:04:36")
        self.assertIsNotNone(status["vswr"])
        # Confirms these were never invented -- see KNOWN_PARAMETERS.
        self.assertNotIn("rf_status", status)
        self.assertNotIn("frequency_hz", status)
        client.__exit__(None, None, None)


class ParseNumericTests(SimpleTestCase):
    def test_bare_integer(self):
        self.assertEqual(parse_numeric("259"), 259.0)

    def test_bare_float(self):
        self.assertEqual(parse_numeric("41.7"), 41.7)

    def test_none_passthrough(self):
        self.assertIsNone(parse_numeric(None))

    def test_non_numeric_text(self):
        self.assertIsNone(parse_numeric("Unknown parameter"))


class ComputeVswrTests(SimpleTestCase):
    def test_matches_the_live_captured_reading(self):
        # 259 W fwd / 4 W rev, the exact live-captured reading.
        vswr = compute_vswr(259.0, 4.0)
        self.assertAlmostEqual(vswr, 1.284, places=2)

    def test_none_when_forward_is_zero_or_missing(self):
        self.assertIsNone(compute_vswr(0, 4))
        self.assertIsNone(compute_vswr(None, 4))
        self.assertIsNone(compute_vswr(259, None))

    def test_none_on_fault_when_reflected_meets_or_exceeds_forward(self):
        self.assertIsNone(compute_vswr(10, 10))
        self.assertIsNone(compute_vswr(10, 15))
