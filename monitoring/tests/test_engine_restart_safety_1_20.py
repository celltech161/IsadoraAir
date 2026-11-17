"""Roadmap 1.20 (revised): Monitoring's protected Restart endpoint and the engine safeguard."""
import time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from library.services import engine_restart_safety as safety
from library.tests.engine_restart_state_support import EngineStateFile
from monitoring.models import MonitorCheck, SystemEvent
from updatecenter.backend_client import BackendRejectedError

ENGINE_UNIT = "isadoraair-engine.service"
REASON = "Operator-confirmed emergency engine restart"


@override_settings(SECURE_SSL_REDIRECT=False)
class MonitoringEngineRestartSafetyTests(TestCase):
    def setUp(self):
        self.state = EngineStateFile(self)
        self.state.write()
        self.engine = MonitorCheck.objects.create(
            name="Engine", kind="systemd", systemd_unit=ENGINE_UNIT, enabled=True)
        self.encoders = MonitorCheck.objects.create(
            name="Encoders", kind="systemd", systemd_unit="isadoraair-encoders.service", enabled=True)
        self.gunicorn = MonitorCheck.objects.create(
            name="Web", kind="systemd", systemd_unit="isadoraair-gunicorn.service", enabled=True)
        self.staff = User.objects.create_user("mon-staff", password="x", is_staff=True)
        self.superuser = User.objects.create_superuser("mon-root", password="x")
        patcher = patch("monitoring.views.UpdaterClient")
        self.broker = patcher.start()
        self.addCleanup(patcher.stop)
        self.broker.return_value.restart_operator_service.return_value = {
            "operation_id": "op-1", "state": "accepted"}
        self.restart = self.broker.return_value.restart_operator_service

    def post(self, user, check=None, query="", **data):
        client = Client()
        client.force_login(user)
        url = reverse("monitoring:api-restart-check", args=[(check or self.engine).pk]) + query
        return client.post(url, data)

    def audit(self, title=None):
        qs = SystemEvent.objects.filter(category="restart_safety").order_by("pk")
        return [e for e in qs if title is None or e.title == title]

    def override(self, user, **extra):
        return self.post(user, override=ENGINE_UNIT, reason=REASON, **extra)

    # ---- quiet / warning states keep today's behaviour
    def test_quiet_engine_restarts_exactly_as_before(self):
        response = self.post(self.staff)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["operation_id"], "op-1")
        self.restart.assert_called_once_with(ENGINE_UNIT)
        self.assertEqual(self.audit(), [])

    def test_manual_mode_only_is_a_warning_and_still_restarts(self):
        self.state.write(manual_mode=True)
        self.assertEqual(self.post(self.staff).status_code, 202)
        self.restart.assert_called_once_with(ENGINE_UNIT)

    # ---- blocked states
    def assert_refused(self, response, *, status=409, decision=safety.OVERRIDE_REQUIRED):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["decision"], decision)
        self.restart.assert_not_called()
        return body

    def test_studio_microphone_live_is_refused_before_the_broker(self):
        self.state.write(mic_live=True)
        body = self.assert_refused(self.post(self.staff))
        self.assertEqual(body["restart_safety"]["reasons"], [safety.STUDIO_MIC_LIVE])
        self.assertFalse(body["override_available"])

    def test_remote_dj_connected_with_microphone_off_is_refused(self):
        self.state.write(remote_dj_connected=True, remote_dj_live=False, mic_live=False,
                         remote_dj_slot="occupied")
        body = self.assert_refused(self.post(self.superuser))
        self.assertEqual(body["restart_safety"]["reasons"], [safety.REMOTE_DJ_CONNECTED])
        self.assertTrue(body["override_available"])

    def test_remote_dj_live_reconnecting_establishing_and_cleaning_are_refused(self):
        for overrides in (
            {"remote_dj_connected": True, "remote_dj_live": True, "remote_dj_slot": "occupied"},
            {"remote_dj_connected": True, "remote_dj_reconnecting": True, "remote_dj_slot": "occupied"},
            {"remote_dj_connection": {"status": "connecting"}},
            {"remote_dj_slot": "cleaning"},
        ):
            with self.subTest(overrides=overrides):
                self.state.write(**overrides)
                self.assert_refused(self.post(self.staff))

    def test_missing_stale_and_malformed_status_is_refused_not_assumed_safe(self):
        cases = {
            "missing": lambda: self.state.remove(),
            "stale": lambda: self.state.write(timestamp=time.time() - 120),
            "malformed": lambda: self.state.write_raw("{broken"),
            "unsupported": lambda: self.state.write(state={"timestamp": time.time(), "transport": "PLAYING"}),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                arrange()
                body = self.assert_refused(self.post(self.staff))
                self.assertEqual(body["restart_safety"]["level"], safety.UNKNOWN)

    def test_refusal_response_leaks_no_session_secrets(self):
        self.state.write(remote_dj_connected=True, remote_dj_slot="occupied")
        response = self.post(self.staff)
        for secret in ("ATTEMPT-SECRET-0123456789", "REQ-SECRET", "gate_generation", "user_id"):
            self.assertNotIn(secret, response.content.decode())

    # ---- override contract
    def test_ordinary_staff_cannot_override_even_with_every_field(self):
        self.state.write(mic_live=True)
        self.assert_refused(self.override(self.staff), status=403, decision=safety.OVERRIDE_FORBIDDEN)

    def test_superuser_without_override_is_refused(self):
        self.state.write(mic_live=True)
        self.assert_refused(self.post(self.superuser))

    def test_superuser_override_needs_a_written_reason(self):
        self.state.write(mic_live=True)
        for reason in ("", "short"):
            with self.subTest(reason=reason):
                self.assert_refused(
                    self.post(self.superuser, override=ENGINE_UNIT, reason=reason),
                    decision=safety.REASON_REQUIRED)

    def test_override_must_name_this_exact_unit(self):
        self.state.write(mic_live=True)
        for value in ("1", "true", "on", "isadoraair-gunicorn.service", "isadoraair-engine", ""):
            with self.subTest(value=value):
                self.assert_refused(self.post(self.superuser, override=value, reason=REASON))

    def test_override_in_query_string_is_ignored(self):
        self.state.write(mic_live=True)
        self.assert_refused(self.post(
            self.superuser, query=f"?override={ENGINE_UNIT}&reason={REASON}"))

    def test_superuser_override_submits_once_audits_and_never_claims_completion(self):
        self.state.write(mic_live=True)
        response = self.override(self.superuser)
        self.assertEqual(response.status_code, 202)
        self.restart.assert_called_once_with(ENGINE_UNIT)
        body = response.json()
        self.assertEqual(body["state"], "accepted")  # the broker's state, not "completed"
        self.assertNotIn("completed", str(body).lower())
        (decision,) = self.audit("Superuser override of engine restart safeguard")
        self.assertEqual(decision.detail["operator"]["username"], "mon-root")
        self.assertEqual(decision.detail["reason"], REASON)
        self.assertEqual(decision.detail["target"], {"unit": ENGINE_UNIT, "check_id": self.engine.pk})
        self.assertEqual(decision.detail["engine_state"]["reasons"], [safety.STUDIO_MIC_LIVE])
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "submitted")
        self.assertEqual(outcome.detail["detail"]["operation_id"], "op-1")
        self.assertIs(outcome.detail["completion_claimed"], False)

    def test_unknown_state_can_be_overridden_by_a_superuser_to_recover_a_failed_engine(self):
        self.state.remove()
        self.assertEqual(self.override(self.superuser).status_code, 202)
        self.restart.assert_called_once_with(ENGINE_UNIT)

    def test_a_previous_override_confers_nothing_on_the_next_request(self):
        self.state.write(mic_live=True)
        self.assertEqual(self.override(self.superuser).status_code, 202)
        self.restart.reset_mock()
        self.assert_refused(self.post(self.superuser))

    def test_unneeded_override_is_not_recorded_as_one(self):
        response = self.override(self.superuser)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.audit(), [])

    # ---- race between page render and POST
    def test_state_is_read_fresh_on_every_post(self):
        self.assertEqual(self.post(self.staff).status_code, 202)
        self.restart.reset_mock()
        self.state.write(mic_live=True)  # DJ keys up after the page rendered
        self.assert_refused(self.post(self.staff))
        self.state.write(mic_live=False)
        self.assertEqual(self.post(self.staff).status_code, 202)

    # ---- broker failure
    def test_failed_protected_broker_submission_is_a_503_and_audited_for_overrides(self):
        self.state.write(mic_live=True)
        self.restart.side_effect = BackendRejectedError("refused", error_code="ProtocolError")
        response = self.override(self.superuser)
        self.assertEqual(response.status_code, 503)
        self.assertIs(response.json()["ok"], False)
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "broker_rejected")
        self.assertIs(outcome.detail["completion_claimed"], False)

    def test_failed_broker_submission_on_a_quiet_engine_is_unchanged(self):
        self.restart.side_effect = BackendRejectedError("refused", error_code="ProtocolError")
        self.assertEqual(self.post(self.staff).status_code, 503)
        self.assertEqual(self.audit(), [])

    # ---- scope: only the engine's own unit is gated
    def test_unrelated_services_restart_exactly_as_before_even_when_the_engine_is_blocked(self):
        self.state.write(mic_live=True, remote_dj_connected=True)
        for check, unit in ((self.encoders, "isadoraair-encoders.service"),
                            (self.gunicorn, "isadoraair-gunicorn.service")):
            with self.subTest(unit=unit):
                self.restart.reset_mock()
                with patch.object(safety, "assess_engine_restart") as assess:
                    response = self.post(self.staff, check=check)
                self.assertEqual(response.status_code, 202)
                self.restart.assert_called_once_with(unit)
                assess.assert_not_called()  # never even reads engine status

    def test_unrelated_service_restart_needs_no_engine_status_at_all(self):
        self.state.remove()
        self.assertEqual(self.post(self.staff, check=self.encoders).status_code, 202)

    def test_authorization_is_still_checked_before_the_safeguard(self):
        outsider = User.objects.create_user("mon-outsider", password="x")
        self.state.write(mic_live=True)
        with patch.object(safety, "assess_engine_restart") as assess:
            response = self.post(outsider)
        self.assertIn(response.status_code, (302, 403))
        assess.assert_not_called()
        self.restart.assert_not_called()
