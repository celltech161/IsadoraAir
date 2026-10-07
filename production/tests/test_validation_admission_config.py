"""2.22B final correction -- the validation service's admission limits have a
finite, safe configuration domain on EVERY route.

Policy: concurrent validations default 2, allowed 1..4; pending validation
requests default 4, allowed 0..8. One canonical parser
(production.services.admission) serves the Django admin form, the service's
settings/.env reading, its command line and the ValidationService
constructor. Out-of-domain values are refused, never clamped or
reinterpreted, and a service given one never starts.
"""
import json
import os
import shutil
import socket
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from isadoraair import env_config
from production.services import admission
from production.services.validation_service import ValidationService

from .support import IsolatedMediaRootMixin
from .validation_service_support import TransientValidationService

ACTIVE_PASS = (1, 2, 4, "1", "2", "4")
PENDING_PASS = (0, 4, 8, "0", "4", "8")
# Values every route must refuse (shared shapes, then each limit's own range edges).
MALFORMED = (
    True, False, 1.5, 2.0, 2.5, float("nan"), Decimal("2"), None, [2], b"2",
    "1.5", "2.5", "2.0", "abc", "", " ", " 2", "2 ", "\t2", "2\n", "+2", "-0", "0x2", "0o2", "2e0", "1_0",
    "02", "٢", "２", "9" * 400, str(10 ** 100), 10 ** 100,
)
ACTIVE_FAIL = MALFORMED + (0, -1, 5, 100, "0", "-1", "5", "100")
PENDING_FAIL = MALFORMED + (-1, 9, 100, "-1", "9", "100")


def _private_socket(testcase):
    directory = tempfile.mkdtemp(prefix="isadoraair-admission-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    os.chmod(directory, 0o700)
    testcase.addCleanup(shutil.rmtree, directory, True)
    return os.path.join(directory, "validator.sock")


class CanonicalDomainTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_the_policy(self):
        self.assertEqual((admission.ACTIVE_DEFAULT, admission.ACTIVE_MIN, admission.ACTIVE_MAX), (2, 1, 4))
        self.assertEqual((admission.PENDING_DEFAULT, admission.PENDING_MIN, admission.PENDING_MAX), (4, 0, 8))

    def test_active_accepts_exactly_1_to_4(self):
        for value in ACTIVE_PASS:
            with self.subTest(value=value):
                self.assertEqual(admission.parse_active(value), int(value))

    def test_active_rejects_everything_else(self):
        for value in ACTIVE_FAIL:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(admission.AdmissionConfigError) as caught:
                admission.parse_active(value)
            self.assertIn("Concurrent validations must be between 1 and 4.", str(caught.exception))

    def test_pending_accepts_exactly_0_to_8(self):
        for value in PENDING_PASS:
            with self.subTest(value=value):
                self.assertEqual(admission.parse_pending(value), int(value))

    def test_pending_rejects_everything_else(self):
        for value in PENDING_FAIL:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(admission.AdmissionConfigError) as caught:
                admission.parse_pending(value)
            self.assertIn("Pending validation requests must be between 0 and 8.", str(caught.exception))

    def test_boundaries(self):
        for parse, below, low, high, above in ((admission.parse_active, 0, 1, 4, 5),
                                               (admission.parse_pending, -1, 0, 8, 9)):
            self.assertEqual((parse(low), parse(high)), (low, high))
            for value in (below, above):
                with self.assertRaises(admission.AdmissionConfigError):
                    parse(value)

    def test_a_huge_value_is_not_echoed(self):
        with self.assertRaises(admission.AdmissionConfigError) as caught:
            admission.parse_active("9" * 400)
        self.assertLess(len(str(caught.exception)), 120)


class ServiceRoutesTests(IsolatedMediaRootMixin, SimpleTestCase):
    """The service's own routes: direct construction, settings/.env, CLI."""

    def test_direct_construction_enforces_the_domain(self):
        path = _private_socket(self)
        for active, pending in ((1, 0), (4, 8), ("2", "4")):
            service = ValidationService(path, max_active=active, max_pending=pending)
            self.assertEqual((service.max_active, service.max_pending), (int(active), int(pending)))
        for value in ACTIVE_FAIL:
            if value is None:
                continue                                    # None = "not given": the configured value
            with self.subTest(active=repr(value)[:40]), self.assertRaises(admission.AdmissionConfigError):
                ValidationService(path, max_active=value, max_pending=4)
        for value in PENDING_FAIL:
            if value is None:
                continue
            with self.subTest(pending=repr(value)[:40]), self.assertRaises(admission.AdmissionConfigError):
                ValidationService(path, max_active=2, max_pending=value)

    def test_defaults_are_unchanged(self):
        service = ValidationService(_private_socket(self))
        self.assertEqual((service.max_active, service.max_pending), (2, 4))

    def test_settings_enforce_the_domain(self):
        path = _private_socket(self)
        with override_settings(PRODUCTION_VALIDATION_MAX_ACTIVE="4", PRODUCTION_VALIDATION_MAX_PENDING="8"):
            self.assertEqual((ValidationService(path).max_active, ValidationService(path).max_pending), (4, 8))
        for value in ACTIVE_FAIL:
            with self.subTest(active=repr(value)[:40]), override_settings(PRODUCTION_VALIDATION_MAX_ACTIVE=value), \
                    self.assertRaises(admission.AdmissionConfigError):
                ValidationService(path)
        for value in PENDING_FAIL:
            with self.subTest(pending=repr(value)[:40]), override_settings(PRODUCTION_VALIDATION_MAX_PENDING=value), \
                    self.assertRaises(admission.AdmissionConfigError):
                ValidationService(path)

    def _command(self, *args):
        """Run the service command; never actually serve. Returns the limits a
        started service would have enforced."""
        started = []
        with mock.patch.object(ValidationService, "serve_forever",
                               lambda service: started.append((service.max_active, service.max_pending))):
            call_command("production_validation_service", f"--socket={_private_socket(self)}", *args)
        self.assertEqual(len(started), 1)
        return started[0]

    def test_the_command_line_enforces_the_domain(self):
        self.assertEqual(self._command(), (2, 4))
        self.assertEqual(self._command("--max-active=1", "--max-pending=0"), (1, 0))
        self.assertEqual(self._command("--max-active=4", "--max-pending=8"), (4, 8))
        # what a shell can pass: the textual form of each refused value
        cli_strings = lambda values: sorted({str(value) for value in values  # noqa: E731
                                             if isinstance(value, (str, int, float))})
        for value in cli_strings(ACTIVE_FAIL):
            with self.subTest(active=value[:40]), self.assertRaises(CommandError) as caught:
                self._command(f"--max-active={value}")
            self.assertIn("Concurrent validations must be between 1 and 4.", str(caught.exception))
        for value in cli_strings(PENDING_FAIL):
            with self.subTest(pending=value[:40]), self.assertRaises(CommandError) as caught:
                self._command(f"--max-pending={value}")
            self.assertIn("Pending validation requests must be between 0 and 8.", str(caught.exception))

    def test_the_command_refuses_bad_environment_values(self):
        for active, pending in (("100", "100"), ("True", "4"), ("False", "4"), ("2.5", "4"),
                                (str(10 ** 100), "4"), ("2", "9"), ("0", "4")):
            with self.subTest(active=active[:20], pending=pending), \
                    override_settings(PRODUCTION_VALIDATION_MAX_ACTIVE=active, PRODUCTION_VALIDATION_MAX_PENDING=pending), \
                    self.assertRaises(CommandError):
                self._command()


class RealServiceRefusesWideAdmissionTests(IsolatedMediaRootMixin, SimpleTestCase):
    """The real unit (the production unit's properties): configuration that
    would widen admission -- or that is not a plain number -- never yields a
    running service. systemd keeps retrying; nothing ever listens."""

    def _never_listens(self, **options):
        service = TransientValidationService(wait=False, **options)
        self.addCleanup(service.stop)
        attempts, accepted = set(), False
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            pid = service.main_pid()
            if pid:
                attempts.add(pid)
            accepted = accepted or service.accepts()
            time.sleep(0.05)
        self.assertFalse(accepted, f"a service started with {options}")
        self.assertFalse(os.path.lexists(service.socket))
        self.assertIsNone(admission.running_limits(service.socket))
        self.assertGreaterEqual(len(attempts), 2, "the start did not fail (and get retried)")
        return service

    def test_huge_limits_on_the_command_line_never_start(self):
        self._never_listens(args=["--max-active=100", "--max-pending=100"])

    def test_huge_or_non_numeric_limits_in_the_environment_never_start(self):
        for active, pending in (("100", "100"), ("True", "4"), ("False", "4"), ("2.5", "4"), (str(10 ** 100), "4")):
            with self.subTest(active=active[:20], pending=pending):
                self._never_listens(env={"PRODUCTION_VALIDATION_MAX_ACTIVE": active,
                                         "PRODUCTION_VALIDATION_MAX_PENDING": pending})

    def test_the_largest_allowed_limits_start_and_are_reported(self):
        service = TransientValidationService(env={"PRODUCTION_VALIDATION_MAX_ACTIVE": "4",
                                                  "PRODUCTION_VALIDATION_MAX_PENDING": "8"})
        self.addCleanup(service.stop)
        self.assertEqual(admission.running_limits(service.socket), {"max_active": 4, "max_pending": 8})
        info = os.stat(admission.status_path(service.socket))
        self.assertEqual(info.st_mode & 0o777, 0o600)


@override_settings(SECURE_SSL_REDIRECT=False)
class ValidationLimitsAdminTests(IsolatedMediaRootMixin, TestCase):
    """Production media -> Validation limits: the shared .env sub-page."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="isadoraair-limits-admin-")
        self.addCleanup(tmp.cleanup)
        self.env_path = Path(tmp.name) / ".env"
        patcher = mock.patch.object(env_config, "ENV_FILE_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.socket = str(Path(tmp.name) / "run" / "validator.sock")
        Path(self.socket).parent.mkdir(mode=0o700)
        socket_override = override_settings(PRODUCTION_VALIDATION_SOCKET=self.socket)
        socket_override.enable()
        self.addCleanup(socket_override.disable)
        self.admin = User.objects.create_superuser("limitsadmin", "limits@example.invalid", "pw")
        self.client.force_login(self.admin)
        self.url = reverse("admin:production_productionmedia_validation_limits")

    def post(self, active="2", pending="4"):
        return self.client.post(self.url, {"production_validation_max_active": active,
                                           "production_validation_max_pending": pending}, follow=True)

    def saved(self):
        return self.env_path.read_text() if self.env_path.exists() else ""

    def test_the_page_shows_both_limits_their_domain_and_purpose(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Concurrent validations (allowed 1-4, default 2)", html)
        self.assertIn("Pending validation requests (allowed 0-8, default 4)", html)
        self.assertIn("protect the on-air host from validation resource exhaustion", html)
        self.assertIn('name="production_validation_max_active" value="2"', html)
        self.assertIn('name="production_validation_max_pending" value="4"', html)
        changelist = self.client.get(reverse("admin:production_productionmedia_changelist")).content.decode()
        self.assertIn(self.url, changelist)

    def test_in_domain_values_are_saved(self):
        for active, pending in (("1", "0"), ("4", "8"), ("2", "4")):
            with self.subTest(active=active, pending=pending):
                html = self.post(active, pending).content.decode()
                self.assertNotIn("Could not save", html)
                self.assertIn(f"PRODUCTION_VALIDATION_MAX_ACTIVE={active}\n", self.saved())
                self.assertIn(f"PRODUCTION_VALIDATION_MAX_PENDING={pending}\n", self.saved())

    def test_out_of_domain_values_are_refused_with_a_useful_message_and_nothing_is_saved(self):
        self.post("3", "5")
        before = self.saved()
        form_strings = sorted({str(value) for value in MALFORMED if isinstance(value, (str, int, float))}
                              - {"2\n"})               # a newline: refused by the shared layer (below)
        for value in form_strings + ["0", "-1", "5", "100"]:
            with self.subTest(active=value[:40]):
                html = self.post(value, "4").content.decode()
                self.assertIn("Could not save", html)
                self.assertIn("Concurrent validations must be between 1 and 4.", html)
                self.assertEqual(self.saved(), before)
        for value in form_strings + ["-1", "9", "100"]:
            with self.subTest(pending=value[:40]):
                html = self.post("2", value).content.decode()
                self.assertIn("Could not save", html)
                self.assertIn("Pending validation requests must be between 0 and 8.", html)
                self.assertEqual(self.saved(), before)
        self.assertIn("Could not save", self.post("2\n", "4").content.decode())
        self.assertEqual(self.saved(), before)

    def test_the_shared_write_path_refuses_them_too(self):
        for key, value in (("PRODUCTION_VALIDATION_MAX_ACTIVE", "5"), ("PRODUCTION_VALIDATION_MAX_ACTIVE", "True"),
                           ("PRODUCTION_VALIDATION_MAX_PENDING", "9"), ("PRODUCTION_VALIDATION_MAX_PENDING", "2.5")):
            with self.subTest(key=key, value=value), self.assertRaises(env_config.InvalidValueError):
                env_config.update_managed_values({key: value})
        self.assertEqual(self.saved(), "")

    def test_restart_required_reflects_what_the_validation_service_enforces(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Restart required", html)                     # not running / not reporting
        admission.write_status(self.socket, 2, 4)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Running configuration matches saved configuration", html)
        html = self.post("3", "4").content.decode()
        self.assertIn("Restart required for these changes to take effect", html)
        admission.write_status(self.socket, 3, 4)                    # after the controlled restart
        self.assertIn("Running configuration matches saved configuration", self.client.get(self.url).content.decode())

    def test_only_a_superuser_may_change_them(self):
        viewer = User.objects.create_user("limitsviewer", "viewer@example.invalid", "pw", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_productionmedia"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url, {"production_validation_max_active": "4",
                                                     "production_validation_max_pending": "8"}).status_code, 403)
        self.assertEqual(self.saved(), "")
        nobody = User.objects.create_user("limitsnobody", "nobody@example.invalid", "pw", is_staff=True)
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class RunningLimitsReportTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_a_missing_or_nonsensical_report_is_unknown_never_a_value(self):
        path = _private_socket(self)
        self.assertIsNone(admission.running_limits(path))
        for payload in (b"", b"[]", b"{}", json.dumps({"max_active": 100, "max_pending": 4}).encode(),
                        json.dumps({"max_active": True, "max_pending": 4}).encode(), b"\xff"):
            Path(admission.status_path(path)).write_bytes(payload)
            with self.subTest(payload=payload):
                self.assertIsNone(admission.running_limits(path))
        admission.write_status(path, 4, 8)
        self.assertEqual(admission.running_limits(path), {"max_active": 4, "max_pending": 8})

    def test_a_listening_service_reports_its_limits_and_withdraws_them_on_stop(self):
        path = _private_socket(self)
        service = ValidationService(path, max_active=3, max_pending=1)
        service.bind()
        self.assertEqual(admission.running_limits(path), {"max_active": 3, "max_pending": 1})
        with mock.patch.object(service, "reap"):
            service.stop()
            service.shutdown()
        self.assertIsNone(admission.running_limits(path))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self.assertRaises(OSError):
            probe.connect(path)
        probe.close()
