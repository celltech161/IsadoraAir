"""Roadmap 1.20 (revised): Update Center install admission and the engine safeguard."""
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from library.services import engine_restart_safety as safety
from library.tests.engine_restart_state_support import EngineStateFile
from monitoring.models import SystemEvent
from updatecenter import views as uc_views
from updatecenter.backend_client import PROTOCOL_VERSION
from updatecenter.job_service import JobSubmissionError

from .test_views import READY_BACKEND, _operator_plan

REASON = "Release must go in during the live show; engineer on site"
RELEASE = "r0005"
FINGERPRINT = "f" * 64


def plan(restarts=("isadoraair-gunicorn", "isadoraair-engine"), **changes):
    values = dict(
        safety_status="ready_to_plan", installed_release_id="r0003", target_release_id=RELEASE,
        installed_commit="a" * 40, target_commit="b" * 40, fingerprint=FINGERPRINT,
        services_requiring_restart=tuple(restarts), releases_in_plan=("r0004", RELEASE),
        minimum_updater_protocol_version=PROTOCOL_VERSION,
    )
    values.update(changes)
    return _operator_plan(**values)


@override_settings(SECURE_SSL_REDIRECT=False)
class UpdateCenterEngineRestartSafetyTests(TestCase):
    def setUp(self):
        self.state = EngineStateFile(self)
        self.state.write()
        self.superuser = User.objects.create_superuser("uc-root", password="x")
        self.staff = User.objects.create_user("uc-staff", password="x", is_staff=True)
        self.job = SimpleNamespace(id=uuid.uuid4())
        self.build_plan = self._patch("updatecenter.views.planner.build_plan", return_value=plan())
        self._patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND)
        self._patch("updatecenter.views.database_maintenance_lock", return_value=nullcontext())
        self._patch("updatecenter.views.database_rotation_is_pending", return_value=False)
        self.create = self._patch("updatecenter.views.create_job", return_value=self.job)
        self.submit = self._patch("updatecenter.views.submit_job", return_value={"ok": True})

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def post(self, user, follow=True, **values):
        client = Client()
        client.force_login(user)
        payload = {"confirmed_target_release_id": RELEASE, "confirmed_plan_fingerprint": FINGERPRINT}
        payload.update(values)
        return client.post(reverse("updatecenter:start-update"), payload, follow=follow)

    def override(self, user=None, **extra):
        return self.post(user or self.superuser, engine_restart_override=RELEASE,
                         engine_restart_override_reason=REASON, **extra)

    def flashed(self, response):
        return " ".join(str(m) for m in response.context["messages"])

    def audit(self, title=None):
        qs = SystemEvent.objects.filter(category="restart_safety").order_by("pk")
        return [e for e in qs if title is None or e.title == title]

    def assert_not_submitted(self, response):
        self.create.assert_not_called()
        self.submit.assert_not_called()
        self.assertIn("Engine restart refused", self.flashed(response))

    # ---- plans that do NOT restart the engine are untouched
    def test_gunicorn_only_plan_is_never_gated_and_reads_no_engine_status(self):
        self.build_plan.return_value = plan(restarts=("isadoraair-gunicorn",))
        self.state.write(mic_live=True, remote_dj_connected=True)
        with patch.object(safety, "assess_engine_restart") as assess:
            self.post(self.superuser)
        assess.assert_not_called()
        self.create.assert_called_once()
        self.submit.assert_called_once_with(self.job)
        self.assertEqual(self.audit(), [])

    def test_gunicorn_only_plan_proceeds_even_with_no_engine_status_file(self):
        self.build_plan.return_value = plan(restarts=("isadoraair-gunicorn",))
        self.state.remove()
        self.post(self.superuser)
        self.submit.assert_called_once()

    def test_plan_without_any_restart_identities_is_untouched(self):
        self.build_plan.return_value = plan(restarts=())
        self.state.write(mic_live=True)
        self.post(self.superuser)
        self.submit.assert_called_once()

    def test_other_non_engine_services_do_not_trigger_the_check(self):
        self.build_plan.return_value = plan(restarts=("isadoraair-encoders", "isadoraair-rbds", "isadoraair-monitoring"))
        self.state.write(mic_live=True)
        self.post(self.superuser)
        self.submit.assert_called_once()

    # ---- plans that DO restart the engine
    def test_quiet_engine_plan_installs_normally(self):
        response = self.post(self.superuser)
        self.create.assert_called_once()
        self.submit.assert_called_once_with(self.job)
        self.assertNotIn("Engine restart refused", self.flashed(response))
        self.assertEqual(self.audit(), [])

    def test_manual_mode_only_is_a_warning_and_installs(self):
        self.state.write(manual_mode=True)
        self.post(self.superuser)
        self.submit.assert_called_once()

    def test_blocked_states_refuse_before_any_job_is_created(self):
        for overrides in (
            {"mic_live": True},
            {"remote_dj_connected": True, "remote_dj_live": False, "remote_dj_slot": "occupied"},
            {"remote_dj_connected": True, "remote_dj_live": True, "remote_dj_slot": "occupied"},
            {"remote_dj_connected": True, "remote_dj_reconnecting": True, "remote_dj_slot": "occupied"},
            {"remote_dj_connection": {"status": "connecting"}},
            {"remote_dj_slot": "cleaning"},
        ):
            with self.subTest(overrides=overrides):
                self.state.write(**overrides)
                self.assert_not_submitted(self.post(self.superuser))
                self.create.reset_mock()
                self.submit.reset_mock()

    def test_missing_stale_or_malformed_status_refuses(self):
        import time
        for arrange in (
            lambda: self.state.remove(),
            lambda: self.state.write(timestamp=time.time() - 300),
            lambda: self.state.write_raw("garbage"),
            lambda: self.state.write(state={"timestamp": time.time()}),
        ):
            arrange()
            self.assert_not_submitted(self.post(self.superuser))
            self.create.reset_mock()
            self.submit.reset_mock()

    def test_refusal_message_states_the_reason_and_the_override_path(self):
        self.state.write(mic_live=True)
        message = self.flashed(self.post(self.superuser))
        self.assertIn("studio microphone is live", message)
        self.assertIn("superuser may override", message)

    # ---- override
    def test_superuser_override_installs_and_audits_everything_required(self):
        self.state.write(mic_live=True)
        response = self.override()
        self.create.assert_called_once()
        self.submit.assert_called_once_with(self.job)
        self.assertNotIn("Engine restart refused", self.flashed(response))
        (decision,) = self.audit("Superuser override of engine restart safeguard")
        self.assertEqual(decision.detail["operator"]["username"], "uc-root")
        self.assertEqual(decision.detail["reason"], REASON)
        self.assertEqual(decision.detail["target"]["release"], RELEASE)
        self.assertEqual(decision.detail["target"]["plan_fingerprint"], FINGERPRINT)
        self.assertIn("isadoraair-engine", decision.detail["target"]["services_requiring_restart"])
        self.assertEqual(decision.detail["engine_state"]["reasons"], [safety.STUDIO_MIC_LIVE])
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "submitted")
        self.assertEqual(outcome.detail["detail"]["job_id"], str(self.job.id))
        self.assertIs(outcome.detail["completion_claimed"], False)

    def test_override_is_bound_to_the_release_being_installed(self):
        self.state.write(mic_live=True)
        for value in ("1", "on", "true", "r0004", "r0006", ""):
            with self.subTest(value=value):
                self.assert_not_submitted(self.post(
                    self.superuser, engine_restart_override=value, engine_restart_override_reason=REASON))
                self.create.reset_mock()
                self.submit.reset_mock()

    def test_override_requires_a_written_reason(self):
        self.state.write(mic_live=True)
        for reason in ("", "   ", "short"):
            with self.subTest(reason=reason):
                self.assert_not_submitted(self.post(
                    self.superuser, engine_restart_override=RELEASE, engine_restart_override_reason=reason))

    def test_unknown_status_can_be_overridden_by_a_superuser(self):
        self.state.remove()
        self.override()
        self.submit.assert_called_once()

    def test_a_previous_override_does_not_carry_to_the_next_install(self):
        self.state.write(mic_live=True)
        self.override()
        self.create.reset_mock()
        self.submit.reset_mock()
        self.assert_not_submitted(self.post(self.superuser))

    def test_ordinary_staff_cannot_start_an_update_or_override(self):
        self.state.write(mic_live=True)
        client = Client()
        client.force_login(self.staff)
        response = client.post(reverse("updatecenter:start-update"), {
            "confirmed_target_release_id": RELEASE, "confirmed_plan_fingerprint": FINGERPRINT,
            "engine_restart_override": RELEASE, "engine_restart_override_reason": REASON})
        self.assertEqual(response.status_code, 403)
        self.create.assert_not_called()
        self.submit.assert_not_called()

    # ---- fresh trusted plan + fingerprint + race
    def test_changed_plan_fingerprint_is_rejected_before_the_safeguard_runs(self):
        self.state.write(mic_live=True)
        with patch.object(safety, "assess_engine_restart") as assess:
            # follow=False: only the POST itself (the redirected dashboard GET
            # legitimately assesses for presentation).
            response = self.post(self.superuser, follow=False, confirmed_plan_fingerprint="e" * 64,
                                 engine_restart_override=RELEASE, engine_restart_override_reason=REASON)
        assess.assert_not_called()
        self.create.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("update plan changed" in str(m) for m in response.wsgi_request._messages))
        self.assertEqual(self.audit(), [])

    def test_plan_that_newly_adds_the_engine_is_gated_on_post(self):
        # The page was rendered for a Gunicorn-only plan (fingerprint F); by POST
        # time the trusted plan restarts the engine under a NEW fingerprint, so
        # the stale confirmation is rejected, and a confirmation of the new plan
        # is gated.
        self.state.write(mic_live=True)
        self.build_plan.return_value = plan(restarts=("isadoraair-gunicorn", "isadoraair-engine"),
                                            fingerprint="9" * 64)
        stale = self.post(self.superuser)  # still confirming F
        self.assertIn("update plan changed", self.flashed(stale))
        fresh = self.post(self.superuser, confirmed_plan_fingerprint="9" * 64)
        self.assert_not_submitted(fresh)

    def test_status_that_changes_between_render_and_post_is_judged_at_post(self):
        # Rendered quiet ...
        page = Client()
        page.force_login(self.superuser)
        html = page.get(reverse("updatecenter:dashboard")).content.decode()
        self.assertIn("Audio engine restart: Safe", html)
        # ... then the Remote DJ connects before the operator confirms.
        self.state.write(remote_dj_connected=True, remote_dj_slot="occupied")
        self.assert_not_submitted(self.post(self.superuser))

    def test_safeguard_uses_the_fresh_plan_not_the_posted_form(self):
        # The form claims nothing about restarts; only the trusted plan decides.
        self.state.write(mic_live=True)
        self.assert_not_submitted(self.post(self.superuser, services_requiring_restart="isadoraair-gunicorn"))

    # ---- submission failures
    def test_failed_protected_submission_is_reported_and_audited_for_overrides(self):
        self.state.write(mic_live=True)
        self.submit.side_effect = JobSubmissionError("protected updater proved the job was never accepted")
        response = self.override()
        self.assertIn("Protected updater rejected the job", self.flashed(response))
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "broker_rejected")
        self.assertIs(outcome.detail["completion_claimed"], False)

    def test_uncertain_submission_is_recorded_as_uncertain_not_success(self):
        self.state.write(mic_live=True)
        self.submit.return_value = {"ok": False, "submission_uncertain": True}
        response = self.override()
        self.assertIn("temporarily unknown", self.flashed(response))
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "submission_uncertain")

    def test_maintenance_lock_refusal_after_override_is_recorded_as_not_submitted(self):
        from isadoraair.maintenance_lock import MaintenanceLockError
        self.state.write(mic_live=True)
        with patch("updatecenter.views.database_maintenance_lock", side_effect=MaintenanceLockError("busy")):
            self.override()
        self.create.assert_not_called()
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "not_submitted_maintenance_lock")

    def test_the_arguments_sent_for_the_protected_job_are_unchanged_by_the_override(self):
        # The safeguard is purely a Django-side admission gate: the plan handed
        # to create_job (and so the protected fingerprint) is the fresh plan.
        self.state.write(mic_live=True)
        fresh = plan()
        self.build_plan.return_value = fresh
        self.override()
        _args, kwargs = self.create.call_args
        self.assertIs(kwargs["plan"], fresh)
        self.assertEqual(kwargs["plan"].fingerprint, FINGERPRINT)

    # ---- presentation
    def render(self, user):
        client = Client()
        client.force_login(user)
        return client.get(reverse("updatecenter:dashboard")).content.decode()

    def test_dashboard_shows_the_reason_and_override_controls_to_a_superuser(self):
        self.state.write(mic_live=True)
        html = self.render(self.superuser)
        self.assertIn("Audio engine restart: Blocked", html)
        self.assertIn("studio microphone is live", html)
        self.assertIn('name="engine_restart_override"', html)
        self.assertIn(f'value="{RELEASE}"', html)
        self.assertIn('name="engine_restart_override_reason"', html)

    def test_dashboard_hides_override_controls_from_non_superusers(self):
        self.state.write(mic_live=True)
        html = self.render(self.staff)
        self.assertNotIn('name="engine_restart_override"', html)

    def test_dashboard_reports_unknown_when_status_is_unavailable(self):
        self.state.remove()
        html = self.render(self.superuser)
        self.assertIn("Audio engine restart: Unknown", html)
        self.assertIn("No engine status is available", html)

    def test_dashboard_for_gunicorn_only_plan_has_no_engine_panel_and_reads_no_status(self):
        self.build_plan.return_value = plan(restarts=("isadoraair-gunicorn",))
        self.state.write(mic_live=True)
        with patch.object(safety, "assess_engine_restart") as assess:
            html = self.render(self.superuser)
        assess.assert_not_called()
        self.assertNotIn("uc-engine-restart-safety", html)

    def test_dashboard_does_not_show_override_controls_when_quiet(self):
        html = self.render(self.superuser)
        self.assertIn("Audio engine restart: Safe", html)
        self.assertNotIn('name="engine_restart_override"', html)
