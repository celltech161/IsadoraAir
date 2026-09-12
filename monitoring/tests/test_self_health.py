"""P1 1.11 -- monitoring/services/self_health.py, the independent
override for the Monitoring Service's OWN dashboard card. Every
`systemctl` call is mocked -- these tests never depend on (or affect)
any real systemd unit."""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from monitoring.models import MonitorCheck
from monitoring.services import self_health


def _systemctl_result(stdout, returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
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

    def test_nonzero_returncode_reports_systemd_unavailable_not_inactive(self):
        """Safety correction: a FAILED systemctl invocation (e.g. the
        system bus unreachable) must be reason=
        "heartbeat_stale_systemd_unavailable", never misclassified as
        reason="service_inactive" -- the latter is a positive claim
        that systemd confirmed the unit is not running, which a failed
        query never actually established."""
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=inactive\nSubState=dead\n", returncode=1),
        ):
            status, detail = self_health._build_override_status(45.0)
        self.assertEqual(status, "critical")
        self.assertEqual(detail["reason"], "heartbeat_stale_systemd_unavailable")
        self.assertNotIn("active_state", detail)


class ProbeMonitoringUnitActiveStateTests(TestCase):
    """Direct unit tests of _probe_monitoring_unit_active_state()'s own
    contract -- returns (active_state, sub_state) ONLY when systemd
    actually, successfully reported a usable state; None in every
    other case. See self_health.py's own docstring for why this
    distinction matters (a failed query must never be confused with a
    successful "not active" report)."""

    def test_normal_active_running_result(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n"),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertEqual(result, ("active", "running"))

    def test_normal_inactive_failed_result(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=failed\nSubState=failed\n"),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertEqual(result, ("failed", "failed"))

    def test_nonzero_return_code_is_none_even_with_active_looking_output(self):
        """The exact bug being corrected: previously, only stdout was
        parsed and returncode was ignored entirely -- a failed
        systemctl invocation that happened to print SOMETHING
        active-state-shaped (e.g. stale/cached output on some systemd
        versions, or a partial write before failing) would have been
        trusted as a real result."""
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=active\nSubState=running\n", returncode=1),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_empty_output_with_return_code_zero_is_none(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("", returncode=0),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_malformed_output_missing_active_state_key_is_none(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("SubState=running\n", returncode=0),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_active_state_present_but_empty_value_is_none(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            return_value=_systemctl_result("ActiveState=\nSubState=\n", returncode=0),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_missing_binary_is_none(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            side_effect=FileNotFoundError("no such binary"),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_timeout_is_none(self):
        import subprocess
        with patch(
            "monitoring.services.self_health.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)

    def test_generic_oserror_is_none(self):
        with patch(
            "monitoring.services.self_health.subprocess.run",
            side_effect=OSError("some other subprocess failure"),
        ):
            result = self_health._probe_monitoring_unit_active_state()
        self.assertIsNone(result)


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
