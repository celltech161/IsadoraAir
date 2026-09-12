"""P1 1.11 -- monitoring/services/self_health.py, the independent
override for the Monitoring Service's OWN dashboard card. Every
`systemctl` call is mocked -- these tests never depend on (or affect)
any real systemd unit."""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from monitoring.models import MonitorCheck
from monitoring.services import self_health


def _systemctl_result(stdout):
    result = MagicMock()
    result.stdout = stdout
    return result


class BuildOverrideStatusTests(TestCase):
    """Direct unit tests of the (status, detail) decision, with
    subprocess.run mocked -- covers the three distinguished cases the
    P1 1.11 roadmap item asks for, plus the fourth ("no heartbeat at
    all yet") this implementation adds."""

    def test_service_inactive_reports_service_inactive_reason(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=inactive\nSubState=dead\n"),
        ):
            status, detail = self_health._build_override_status(45.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "service_inactive")
        self.assertEqual(detail["active_state"], "inactive")
        self.assertEqual(detail["heartbeat_age_seconds"], 45.0)

    def test_failed_service_also_reports_service_inactive(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=failed\nSubState=failed\n"),
        ):
            status, detail = self_health._build_override_status(90.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "service_inactive")

    def test_active_but_stale_heartbeat_reports_process_alive_reason(self):
        """The P1 1.11 poster case: systemd insists the unit is
        running, but the heartbeat says otherwise -- a stale heartbeat
        must ALWAYS win, never be downgraded back toward healthy."""
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
        ):
            status, detail = self_health._build_override_status(120.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "heartbeat_stale_process_alive")
        self.assertEqual(detail["active_state"], "active")

    def test_active_with_no_heartbeat_at_all_is_distinguished(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
        ):
            status, detail = self_health._build_override_status(None)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "no_heartbeat_recorded")

    def test_systemctl_unavailable_reports_systemd_unavailable_reason(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            side_effect=FileNotFoundError("no such binary"),
        ):
            status, detail = self_health._build_override_status(200.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "heartbeat_stale_systemd_unavailable")
        self.assertNotIn("active_state", detail)

    def test_systemctl_timeout_reports_systemd_unavailable_reason(self):
        import subprocess
        with patch(
            "monitoring.services.self_health.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
        ):
            status, detail = self_health._build_override_status(200.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "heartbeat_stale_systemd_unavailable")

    def test_never_reports_ok_regardless_of_active_state(self):
        """A stale heartbeat is unconditionally unhealthy -- systemd's
        ActiveState can only add detail, never restore "ok"."""
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
        ):
            status, _detail = self_health._build_override_status(31.0)
        self.assertNotEqual(status, "ok")


class ApplySelfHealthOverrideTests(TestCase):
    """apply_self_health_override()'s own list-rewriting contract --
    override the matching row by systemd_unit, never by name; leave
    every other row completely alone; synthesize from MonitorCheck
    config when no matching row exists at all."""

    def _mock_active(self):
        return patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
        )

    def test_matching_row_is_overridden_by_systemd_unit_not_name(self):
        checks = [
            {"id": 1, "name": "Anything An Operator Renamed It To",
             "kind": "systemd", "status": "ok", "detail": {"active_state": "active"},
             "systemd_unit": self_health.MONITORING_UNIT},
        ]
        with self._mock_active():
            result = self_health.apply_self_health_override(checks, 999.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "critical")
        self.assertEqual(result[0]["detail"]["reason"], "heartbeat_stale_process_alive")

    def test_other_checks_are_completely_untouched(self):
        checks = [
            {"id": 1, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "detail": {"active_state": "active"},
             "systemd_unit": "isadoraair-engine.service"},
            {"id": 2, "name": "Monitoring Service", "kind": "systemd",
             "status": "ok", "detail": {}, "systemd_unit": self_health.MONITORING_UNIT},
        ]
        with self._mock_active():
            result = self_health.apply_self_health_override(checks, 999.0)
        engine = next(c for c in result if c["systemd_unit"] == "isadoraair-engine.service")
        self.assertEqual(engine["status"], "ok")
        self.assertEqual(engine["detail"], {"active_state": "active"})

    def test_input_list_is_not_mutated_in_place(self):
        original_detail = {"active_state": "active"}
        checks = [
            {"id": 1, "name": "Monitoring Service", "kind": "systemd",
             "status": "ok", "detail": original_detail, "systemd_unit": self_health.MONITORING_UNIT},
        ]
        with self._mock_active():
            self_health.apply_self_health_override(checks, 999.0)
        self.assertEqual(checks[0]["status"], "ok")
        self.assertIs(checks[0]["detail"], original_detail)

    def test_no_matching_check_and_none_configured_returns_unchanged(self):
        checks = [
            {"id": 1, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "detail": {}, "systemd_unit": "isadoraair-engine.service"},
        ]
        with self._mock_active():
            result = self_health.apply_self_health_override(checks, 999.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["systemd_unit"], "isadoraair-engine.service")

    def test_empty_checks_with_no_configured_monitoring_check_stays_empty(self):
        with self._mock_active():
            result = self_health.apply_self_health_override([], None)
        self.assertEqual(result, [])

    def test_synthesizes_from_config_when_missing_from_checks(self):
        check = MonitorCheck.objects.create(
            name="Monitoring Service", kind="systemd",
            systemd_unit=self_health.MONITORING_UNIT, sort_order=5,
        )
        with self._mock_active():
            result = self_health.apply_self_health_override([], None)
        self.assertEqual(len(result), 1)
        synthesized = result[0]
        self.assertEqual(synthesized["id"], check.id)
        self.assertEqual(synthesized["name"], "Monitoring Service")
        self.assertEqual(synthesized["systemd_unit"], self_health.MONITORING_UNIT)
        self.assertEqual(synthesized["status"], "critical")
        self.assertEqual(synthesized["detail"]["reason"], "no_heartbeat_recorded")

    def test_never_synthesizes_for_a_disabled_check(self):
        MonitorCheck.objects.create(
            name="Monitoring Service", kind="systemd",
            systemd_unit=self_health.MONITORING_UNIT, enabled=False,
        )
        with self._mock_active():
            result = self_health.apply_self_health_override([], None)
        self.assertEqual(result, [])

    def test_never_synthesizes_for_a_non_systemd_kind(self):
        """Defense in depth: even if a station somehow has a
        differently-kinded check whose systemd_unit field happens to
        be set (not normally possible via clean()), synthesis stays
        scoped to kind="systemd"."""
        MonitorCheck.objects.create(
            name="Weirdly Configured", kind="disk", disk_path="/",
        )
        with self._mock_active():
            result = self_health.apply_self_health_override([], None)
        self.assertEqual(result, [])

    def test_synthesized_check_preserves_show_as_card(self):
        MonitorCheck.objects.create(
            name="Monitoring Service", kind="systemd",
            systemd_unit=self_health.MONITORING_UNIT, show_as_card=False,
        )
        with self._mock_active():
            result = self_health.apply_self_health_override([], None)
        self.assertFalse(result[0]["show_as_card"])
