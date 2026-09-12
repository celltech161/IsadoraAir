"""P1 1.11 -- monitoring/views.py's api_monitoring_status wiring of
self_health.apply_self_health_override. Real requests through the URL
(reverse("monitoring:api-status")), same pattern
test_release_status.py's ApiMonitoringStatusVersionMergeTests already
established. STATE_PATH is redirected to a temp file for every test --
never touches the real /run/isadoraair/monitoring_state.json a live
isadoraair-monitoring service may also be writing to. Every
`systemctl` call is mocked."""
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.models import MonitorCheck
from monitoring.services import self_health


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _systemctl_result(stdout):
    result = MagicMock()
    result.stdout = stdout
    return result


_ACTIVE_MOCK = patch(
    "monitoring.services.self_health.subprocess.run",
    return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
)
_INACTIVE_MOCK = patch(
    "monitoring.services.self_health.subprocess.run",
    return_value=_systemctl_result("ActiveState=inactive\nSubState=dead\n"),
)


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the plain-HTTP test client would otherwise 301
class ApiStatusSelfHealthOverrideTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staffuser2", password="x", is_staff=True)
        self.client.force_login(self.staff)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "monitoring_state.json"

        from monitoring import views as monitoring_views
        patcher = patch.object(monitoring_views, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Names deliberately distinct from the migration-seeded default
        # checks (monitoring/migrations/0002_seed_default_checks.py
        # also creates a "Playback Engine"/"nginx" row, and
        # MonitorCheck.name is unique) -- same convention
        # test_release_status.py's ApiMonitoringStatusVersionMergeTests
        # already established.
        self.mon_check = MonitorCheck.objects.create(
            name="Test Monitoring Service Check", kind="systemd",
            systemd_unit=self_health.MONITORING_UNIT, sort_order=9,
        )
        self.engine_check = MonitorCheck.objects.create(
            name="Test Playback Engine Check", kind="systemd",
            systemd_unit="isadoraair-engine.service", sort_order=1,
        )

    def _write_state(self, checks, timestamp=None):
        _write(self.state_path, {
            "timestamp": timestamp if timestamp is not None else time.time(),
            "checks": checks,
            "runtime_commit": "abc123",
        })

    def test_fresh_state_is_completely_unaffected(self):
        """Core requirement #1: fresh Monitoring state must continue
        behaving exactly as today -- no self_health override logic
        runs at all (proven by NOT mocking subprocess.run here; a real
        call would either fail loudly in a sandboxed test environment
        or, if it somehow succeeded, this test would still pass since
        the override path must never even be reached)."""
        self._write_state([
            {"id": self.mon_check.id, "name": "Monitoring Service", "kind": "systemd",
             "status": "ok", "detail": {"uptime_seconds": 500},
             "systemd_unit": self_health.MONITORING_UNIT},
        ])
        resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertFalse(data["stale"])
        mon = next(c for c in data["checks"] if c["systemd_unit"] == self_health.MONITORING_UNIT)
        self.assertEqual(mon["status"], "ok")
        self.assertEqual(mon["detail"], {"uptime_seconds": 500})

    def test_stale_state_overrides_previously_green_monitoring_card(self):
        """The exact failure mode this whole feature exists to close:
        an old GREEN 'Running' Monitoring Service card must NEVER
        survive once the overall state is stale."""
        self._write_state(
            [
                {"id": self.mon_check.id, "name": "Monitoring Service", "kind": "systemd",
                 "status": "ok", "detail": {"uptime_seconds": 99999},
                 "systemd_unit": self_health.MONITORING_UNIT},
            ],
            timestamp=time.time() - 120,  # well past STATE_STALE_SECONDS
        )
        with _ACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertTrue(data["stale"])
        mon = next(c for c in data["checks"] if c["systemd_unit"] == self_health.MONITORING_UNIT)
        self.assertEqual(mon["status"], "critical")
        self.assertNotEqual(mon["detail"], {"uptime_seconds": 99999})

    def test_stale_state_other_checks_status_unaffected(self):
        """Only the Monitoring Service's OWN card is overridden -- a
        stale OVERALL state doesn't touch any other check's reported
        status (the page-level banner already communicates the
        general staleness)."""
        self._write_state(
            [
                {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
                 "status": "ok", "detail": {"uptime_seconds": 12345},
                 "systemd_unit": "isadoraair-engine.service"},
                {"id": self.mon_check.id, "name": "Monitoring Service", "kind": "systemd",
                 "status": "ok", "detail": {}, "systemd_unit": self_health.MONITORING_UNIT},
            ],
            timestamp=time.time() - 120,
        )
        with _ACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        engine = next(c for c in data["checks"] if c["systemd_unit"] == "isadoraair-engine.service")
        self.assertEqual(engine["status"], "ok")
        self.assertEqual(engine["detail"], {"uptime_seconds": 12345})

    def test_stale_state_service_inactive_distinguished(self):
        self._write_state(
            [{"id": self.mon_check.id, "name": "Monitoring Service", "kind": "systemd",
              "status": "ok", "detail": {}, "systemd_unit": self_health.MONITORING_UNIT}],
            timestamp=time.time() - 120,
        )
        with _INACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        mon = next(c for c in data["checks"] if c["systemd_unit"] == self_health.MONITORING_UNIT)
        self.assertEqual(mon["detail"]["reason"], "service_inactive")

    def test_missing_state_file_synthesizes_configured_monitoring_card(self):
        """Core requirement #2's hardest case: no state file at all
        (freshly installed station, or the whole file vanished) must
        still show a truthful Monitoring Service card when one is
        actually configured -- not an empty section."""
        with _ACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertTrue(data["stale"])
        mon = next(c for c in data["checks"] if c["systemd_unit"] == self_health.MONITORING_UNIT)
        self.assertEqual(mon["status"], "critical")
        self.assertEqual(mon["detail"]["reason"], "no_heartbeat_recorded")

    def test_missing_state_file_with_no_configured_check_stays_empty(self):
        """Never invents a card nobody configured."""
        self.mon_check.delete()
        with _ACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertEqual(data["checks"], [])
        self.assertTrue(data["stale"])

    def test_malformed_state_file_synthesizes_configured_monitoring_card(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not valid json", encoding="utf-8")
        with _ACTIVE_MOCK:
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertTrue(data["stale"])
        mon = next(c for c in data["checks"] if c["systemd_unit"] == self_health.MONITORING_UNIT)
        self.assertEqual(mon["status"], "critical")

    def test_missing_state_file_still_has_no_checkout_key(self):
        """Pre-existing early-return shape preserved exactly -- see
        test_release_status.py's own
        test_missing_state_file_still_returns_stale_true_no_checkout_crash
        for the ORIGINAL guarantee this test extends."""
        self.mon_check.delete()
        resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertNotIn("checkout", data)

    def test_systemctl_never_invoked_on_fresh_poll(self):
        """Requirement: normal 5-second browser polling must not spawn
        an unnecessary subprocess per tab -- only the stale path may
        probe systemd."""
        self._write_state([
            {"id": self.mon_check.id, "name": "Monitoring Service", "kind": "systemd",
             "status": "ok", "detail": {}, "systemd_unit": self_health.MONITORING_UNIT},
        ])
        with patch("monitoring.services.self_health.subprocess.run") as mock_run:
            resp = self.client.get(reverse("monitoring:api-status"))
        mock_run.assert_not_called()
        self.assertFalse(resp.json()["stale"])
