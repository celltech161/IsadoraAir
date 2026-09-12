"""P1 1.11 -- monitoring/services/sd_notify.py, the stdlib-only systemd
notification helper. Every test controls $NOTIFY_SOCKET/$WATCHDOG_USEC/
$WATCHDOG_PID explicitly via mock.patch.dict(os.environ, ..., clear=...)
-- never depends on (or touches) whatever real systemd environment this
test process happens to be running under."""
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from monitoring.services import sd_notify


class NoSystemdIsAHarmlessNoOpTests(SimpleTestCase):
    """The overwhelming common case in dev/CI/a plain terminal
    `manage.py run_monitoring` -- no $NOTIFY_SOCKET at all."""

    def setUp(self):
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_notify_returns_false_with_no_notify_socket(self):
        self.assertFalse(sd_notify.notify(watchdog=True))

    def test_notify_never_raises_with_no_notify_socket(self):
        # Every call shape a real caller might use.
        self.assertFalse(sd_notify.notify(ready=True))
        self.assertFalse(sd_notify.notify(stopping=True))
        self.assertFalse(sd_notify.notify(status="hello"))
        self.assertFalse(sd_notify.notify())

    def test_watchdog_enabled_false_with_no_watchdog_usec(self):
        self.assertFalse(sd_notify.watchdog_enabled())

    def test_watchdog_interval_seconds_none_with_no_watchdog_usec(self):
        self.assertIsNone(sd_notify.watchdog_interval_seconds())


class RealNotifySocketDeliveryTests(SimpleTestCase):
    """Uses a REAL AF_UNIX SOCK_DGRAM socket bound to a temp path --
    proves the actual wire behavior (message content, framing) rather
    than only mocking `socket.socket` -- without touching any real
    system/user systemd instance."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.socket_path = str(Path(self.tmp_dir.name) / "notify.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.bind(self.socket_path)
        self.server.settimeout(2.0)
        self.addCleanup(self.server.close)

        patcher = patch.dict(os.environ, {"NOTIFY_SOCKET": self.socket_path}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_watchdog_notify_delivers_expected_datagram(self):
        sent = sd_notify.notify(watchdog=True)
        self.assertTrue(sent)
        data, _addr = self.server.recvfrom(4096)
        self.assertEqual(data, b"WATCHDOG=1")

    def test_ready_and_status_combine_into_one_datagram(self):
        sent = sd_notify.notify(ready=True, status="hello world")
        self.assertTrue(sent)
        data, _addr = self.server.recvfrom(4096)
        self.assertEqual(data, b"READY=1\nSTATUS=hello world")

    def test_status_strips_embedded_newlines(self):
        sd_notify.notify(status="line one\nline two\rline three")
        data, _addr = self.server.recvfrom(4096)
        self.assertNotIn(b"\n", data.split(b"=", 1)[1])
        self.assertEqual(data, b"STATUS=line one line two line three")

    def test_no_fields_requested_sends_nothing(self):
        sent = sd_notify.notify()
        self.assertFalse(sent)
        with self.assertRaises(socket.timeout):
            self.server.settimeout(0.2)
            self.server.recvfrom(4096)

    def test_stopping_notify_delivers(self):
        sent = sd_notify.notify(stopping=True)
        self.assertTrue(sent)
        data, _addr = self.server.recvfrom(4096)
        self.assertEqual(data, b"STOPPING=1")


class AbstractNamespaceSocketTests(SimpleTestCase):
    """systemd's own convention: a leading '@' in $NOTIFY_SOCKET means
    the Linux ABSTRACT namespace, not a real filesystem path -- widely
    used by systemd itself (e.g. modern distros' default
    $NOTIFY_SOCKET is often abstract). Confirms the '\\0'-prefix
    translation this module performs actually round-trips against a
    real abstract-namespace socket."""

    def setUp(self):
        # Abstract sockets are process/namespace-scoped and don't
        # collide across parallel test runs the way a fixed filesystem
        # path could -- unique per test process via os.getpid().
        self.abstract_name = f"isadoraair-test-notify-{os.getpid()}"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.bind("\0" + self.abstract_name)
        self.server.settimeout(2.0)
        self.addCleanup(self.server.close)

        patcher = patch.dict(
            os.environ, {"NOTIFY_SOCKET": "@" + self.abstract_name}, clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_at_prefixed_socket_resolves_to_abstract_namespace(self):
        sent = sd_notify.notify(watchdog=True)
        self.assertTrue(sent)
        data, _addr = self.server.recvfrom(4096)
        self.assertEqual(data, b"WATCHDOG=1")


class DeliveryFailureIsolationTests(SimpleTestCase):
    """NOTIFY_SOCKET points somewhere real but nothing is listening (or
    the path doesn't exist) -- notify() must return False, never raise.
    See sd_notify.notify's own docstring for why this is the correct,
    deliberate failure domain (fail toward "systemd's watchdog timer
    elapses," never toward "the calling health-critical code crashes")."""

    def test_missing_socket_path_returns_false_not_raise(self):
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "/nonexistent/path/does/not/exist.sock"}, clear=True):
            self.assertFalse(sd_notify.notify(watchdog=True))

    def test_empty_notify_socket_is_treated_as_absent(self):
        with patch.dict(os.environ, {"NOTIFY_SOCKET": ""}, clear=True):
            self.assertFalse(sd_notify.notify(watchdog=True))


class WatchdogEnabledPidMatchingTests(SimpleTestCase):
    """sd_watchdog_enabled(3)'s own documented contract: WATCHDOG_PID,
    when present, must match our own pid, or this process must NOT
    consider itself the one being watched."""

    def test_enabled_when_watchdog_usec_set_and_no_pid_constraint(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000"}, clear=True):
            self.assertTrue(sd_notify.watchdog_enabled())

    def test_enabled_when_pid_matches_our_own(self):
        with patch.dict(
            os.environ,
            {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid())},
            clear=True,
        ):
            self.assertTrue(sd_notify.watchdog_enabled())

    def test_disabled_when_pid_does_not_match_our_own(self):
        """A forked child (or an unrelated process) inheriting the
        parent's environment must NOT conclude it's the one being
        watched -- systemd is watching the PID it started."""
        other_pid = os.getpid() + 1
        with patch.dict(
            os.environ,
            {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(other_pid)},
            clear=True,
        ):
            self.assertFalse(sd_notify.watchdog_enabled())

    def test_disabled_when_watchdog_usec_is_zero(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "0"}, clear=True):
            self.assertFalse(sd_notify.watchdog_enabled())

    def test_disabled_when_watchdog_usec_is_not_numeric(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "not-a-number"}, clear=True):
            self.assertFalse(sd_notify.watchdog_enabled())

    def test_watchdog_interval_seconds_converts_usec_to_seconds(self):
        with patch.dict(os.environ, {"WATCHDOG_USEC": "60000000"}, clear=True):
            self.assertEqual(sd_notify.watchdog_interval_seconds(), 60.0)

    def test_watchdog_interval_seconds_none_when_pid_mismatched(self):
        with patch.dict(
            os.environ,
            {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid() + 1)},
            clear=True,
        ):
            self.assertIsNone(sd_notify.watchdog_interval_seconds())
