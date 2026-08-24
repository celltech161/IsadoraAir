"""/updates/ permission + side-effect tests -- [P0] 1.1 Phase A."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from updatecenter import views as uc_views
from updatecenter.models import UpdateJob, UpdateJobState
from .gitfixtures import FakeRepo


READY_BACKEND = {
    "reachable": True,
    "protocol_compatible": True,
    "protected_runtime_valid": True,
    "config_valid": True,
    "trusted_repository_ready": True,
    "execution_armed": True,
    "ready": True,
    "detail": "Protected updater is reachable, compatible, ready, and armed.",
}


def _operator_plan(**changes):
    values = {
        "safety_status": "up_to_date",
        "safety_detail": "Installed source is already the latest declared release.",
        "installed_release_id": "r0004",
        "installed_commit": "a" * 40,
        "target_release_id": "r0004",
        "target_commit": "a" * 40,
        "releases_in_plan": (),
        "migrations": None,
        "python_requirements_changed": False,
        "apt_packages_new": (),
        "systemd_units_changed": (),
        "systemd_units_new_required": (),
        "systemd_units_new_optional": (),
        "systemd_units_removed_or_renamed": (),
        "collectstatic_required": False,
        "services_requiring_restart": (),
        "nginx_changed": False,
        "runtime_components_changed": False,
        "minimum_updater_protocol_version": 3,
        "manual_bootstrap_required": False,
        "cross_check_findings": (),
        "fingerprint": "f" * 64,
        "schema_health_status": "schema_current",
        "schema_pending_migrations": (),
        "schema_health_detail": "Database schema is current.",
        "target_schema_validation_status": "not_applicable_no_target_transition",
        "target_schema_validation_detail": "No target transition exists.",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _write_bootstrap_manifest(repo):
    releases_dir = repo.work / "deploy" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1, "release_id": "r0001", "previous_release_id": None,
        "bootstrap_commit": repo.rev_parse("HEAD"), "minimum_updater_protocol_version": 1,
        "summary": "test", "migrations_required": [], "migration_compatibility": None,
        "python_requirements_changed": False, "requirements_sha256": None,
        "apt_packages_new": [], "systemd_units_changed": [], "systemd_units_new_required": [],
        "systemd_units_new_optional": [], "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False, "services_requiring_restart": [],
        "nginx_changed": False, "runtime_components_changed": False,
    }
    (releases_dir / "r0001.json").write_text(json.dumps(data), encoding="utf-8")
    repo.commit("add bootstrap manifest", push=True)
    return releases_dir


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; plain-HTTP test client would otherwise 301
class PermissionTests(TestCase):
    def setUp(self):
        self.anon_client = Client()
        self.plain_user = User.objects.create_user("plainuser", password="x")
        self.staff_user = User.objects.create_user("staffuser", password="x", is_staff=True)
        self.superuser = User.objects.create_superuser("superuser", password="x")

    def test_anonymous_denied(self):
        resp = self.anon_client.get(reverse("updatecenter:dashboard"))
        # LoginRequiredMiddleware redirects anonymous users to the login
        # page (302) before this view's own permission check even runs.
        self.assertIn(resp.status_code, (302, 403))

    def test_ordinary_authenticated_non_staff_denied(self):
        """A plain authenticated user with no recognized group is
        actually denied TWICE over in this project: library.middleware.
        GroupBasedAccessMiddleware (pre-existing, unrelated to this
        app) redirects any authenticated user outside its group-access
        allowlist to /welcome/ before this view's own
        staff-or-superuser check would even run. Either outcome proves
        the page isn't reachable -- this test accepts both rather than
        asserting one specific middleware's behavior, which this app
        doesn't own."""
        client = Client()
        client.force_login(self.plain_user)
        resp = client.get(reverse("updatecenter:dashboard"))
        self.assertIn(resp.status_code, (302, 403))

    def test_staff_view_allowed(self):
        client = Client()
        client.force_login(self.staff_user)
        resp = client.get(reverse("updatecenter:dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_superuser_view_allowed(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(reverse("updatecenter:dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_non_staff_denied_check_for_updates_too(self):
        client = Client()
        client.force_login(self.plain_user)
        resp = client.post(reverse("updatecenter:check-for-updates"))
        self.assertIn(resp.status_code, (302, 403))  # see test_ordinary_authenticated_non_staff_denied


class NoExecutionEndpointTests(TestCase):
    """Proves there is no working "Update IsadoraAir" URL anywhere in
    this app -- not "the button is disabled in HTML" but "the endpoint
    literally does not exist to route to.\""""

    def test_start_update_url_now_exists(self):
        self.assertEqual(reverse("updatecenter:start-update"), "/updates/start/")

    def test_only_narrow_phase_c_urls_are_registered(self):
        from updatecenter.urls import urlpatterns
        names = sorted(p.name for p in urlpatterns)
        self.assertEqual(names, ["check-for-updates", "dashboard", "job-status", "start-update"])


@override_settings(SECURE_SSL_REDIRECT=False)
class GetHasNoSideEffectTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user("staffuser", password="x", is_staff=True)
        self.client_ = Client()
        self.client_.force_login(self.staff_user)

    def test_get_dashboard_never_fetches(self):
        with patch("updatecenter.planner.fetch_updates") as mock_fetch:
            resp = self.client_.get(reverse("updatecenter:dashboard"))
            self.assertEqual(resp.status_code, 200)
            mock_fetch.assert_not_called()

    def test_get_on_check_for_updates_is_rejected(self):
        """GET must never be able to trigger the fetch -- only POST."""
        resp = self.client_.get(reverse("updatecenter:check-for-updates"))
        self.assertEqual(resp.status_code, 405)

    def test_post_check_for_updates_calls_fetch_exactly_once(self):
        with patch("updatecenter.planner.fetch_updates") as mock_fetch:
            mock_fetch.return_value = True
            resp = self.client_.post(reverse("updatecenter:check-for-updates"))
            self.assertEqual(resp.status_code, 302)  # redirect back to dashboard
            mock_fetch.assert_called_once()

    def test_post_without_csrf_token_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff_user)
        with patch("updatecenter.planner.fetch_updates") as mock_fetch:
            resp = csrf_client.post(reverse("updatecenter:check-for-updates"))
            self.assertEqual(resp.status_code, 403)
            mock_fetch.assert_not_called()


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardContentTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user("staffuser", password="x", is_staff=True)
        self.superuser = User.objects.create_superuser("rootuser", password="x")
        self.client_ = Client()
        self.client_.force_login(self.staff_user)

    def test_renders_against_fake_up_to_date_repo(self):
        with FakeRepo() as repo:
            releases_dir = _write_bootstrap_manifest(repo)
            with patch.object(uc_views, "CHECKOUT_ROOT", repo.work), \
                 patch.object(uc_views, "RELEASES_DIRNAME", "deploy/releases"), \
                 patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
                resp = self.client_.get(reverse("updatecenter:dashboard"))
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertIn("Up to date — r0001", body)
                self.assertIn("Current release", body)
                self.assertIn("Update service", body)
                self.assertIn("Ready", body)
                self.assertIn("Check for Updates", body)
                self.assertIn("Technical details", body)
                self.assertIn(f'href="{reverse("monitoring:dashboard")}"', body)
                self.assertIn("System monitoring", body)
                self.assertNotIn("Installed Source", body)
                self.assertNotIn("Running Software", body)
                primary = body.split('<details class="uc-details">', 1)[0]
                self.assertNotIn("Plan fingerprint", primary)
                self.assertNotIn("Target schema validation", primary)
                self.assertNotIn("Roll Back", body)
                self.assertNotIn("Rollback", body)

    def test_update_service_failures_remain_immediately_actionable(self):
        states = (
            "Protected updater is unavailable.",
            "Protected updater is reachable but root execution is disarmed.",
            "Protected updater protocol is incompatible; manual helper upgrade required.",
        )
        for detail in states:
            with self.subTest(detail=detail), \
                 patch("updatecenter.views.planner.build_plan", return_value=_operator_plan()), \
                 patch("updatecenter.views._backend_readiness", return_value={
                     **READY_BACKEND, "ready": False, "detail": detail,
                 }):
                body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
            self.assertIn("Update service needs attention", body)
            self.assertIn(detail, body)
            self.assertIn("Attention required", body)

    def test_schema_problems_remain_visible_and_blocking(self):
        for state in (
            "schema_drift_detected",
            "unapplied_migrations_detected",
            "migration_state_indeterminate",
        ):
            detail = f"Database problem: {state}"
            plan = _operator_plan(
                schema_health_status=state,
                schema_health_detail=detail,
                schema_pending_migrations=("library.9999_example",),
            )
            with self.subTest(state=state), \
                 patch("updatecenter.views.planner.build_plan", return_value=plan), \
                 patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
                body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
            self.assertIn("Database needs attention", body)
            self.assertIn(detail, body)
            self.assertIn("library.9999_example", body)

    def test_update_available_is_concise_and_install_uses_existing_eligibility(self):
        plan = _operator_plan(
            safety_status="ready_to_plan",
            safety_detail="One release is available.",
            target_release_id="r0005",
            target_commit="b" * 40,
            releases_in_plan=("r0005",),
            services_requiring_restart=("isadoraair-gunicorn",),
            target_schema_validation_status="target_schema_plan_validation_pending",
            target_schema_validation_detail="Root will validate the target graph.",
        )
        root_client = Client()
        root_client.force_login(self.superuser)
        patches = (
            patch("updatecenter.views.planner.build_plan", return_value=plan),
            patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND),
        )
        with patches[0], patches[1]:
            body = root_client.get(reverse("updatecenter:dashboard")).content.decode()
        self.assertIn("Update available — r0005", body)
        self.assertIn("r0004 → r0005", body)
        self.assertIn("Database changes", body)
        self.assertIn("Dependencies / system packages", body)
        primary_summary, technical_plan = body.split("<summary>Technical update plan</summary>", 1)
        self.assertIn("Web interface", primary_summary)
        self.assertNotIn("isadoraair-gunicorn", primary_summary)
        self.assertIn("isadoraair-gunicorn", technical_plan)
        self.assertIn(">Install r0005</button>", body)
        self.assertIn("Technical update plan", body)

        with patch("updatecenter.views.planner.build_plan", return_value=plan), \
             patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
            staff_body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
        self.assertNotIn(">Install r0005</button>", staff_body)
        self.assertIn("Only a superuser may start an update.", staff_body)

    def test_known_restart_services_have_operator_labels(self):
        plan = _operator_plan(services_requiring_restart=(
            "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
            "isadoraair-encoders", "isadoraair-rbds",
        ))
        self.assertEqual(
            uc_views._operator_service_restart_labels(plan),
            ("Web interface", "Audio engine", "Monitoring", "Stream encoders", "RBDS"),
        )

    def test_active_job_keeps_progress_and_polling(self):
        job = UpdateJob.objects.create(
            initiated_by_username="rootuser", installed_release_id="r0004",
            target_release_id="r0005", installed_commit="a" * 40,
            target_commit="b" * 40, state=UpdateJobState.RUNNING,
            active_lock=1, current_step="target_staged", progress_detail="Installing safely",
        )
        with patch("updatecenter.views.planner.build_plan", return_value=_operator_plan()), \
             patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND), \
             patch("updatecenter.views._refresh_job", return_value=(job, None)):
            body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
        self.assertIn("Update in progress — r0005", body)
        self.assertIn("target_staged", body)
        self.assertIn("data-status-url", body)
        self.assertIn("window.setTimeout(poll, delay)", body)

    def test_completed_job_is_compact_and_failure_stays_visible(self):
        completed = UpdateJob.objects.create(
            initiated_by_username="rootuser", installed_release_id="r0004",
            target_release_id="r0005", installed_commit="a" * 40,
            target_commit="b" * 40, state=UpdateJobState.SUCCEEDED,
            completed_log_snapshot="completed log details",
        )
        with patch("updatecenter.views.planner.build_plan", return_value=_operator_plan()), \
             patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
            body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
        self.assertIn("Last update", body)
        self.assertIn("Last update details", body)
        self.assertIn("completed log details", body)
        self.assertNotIn("data-status-url", body)

        completed.state = UpdateJobState.FAILED
        completed.failure_detail = "Postflight health check failed"
        completed.save(update_fields=["state", "failure_detail"])
        with patch("updatecenter.views.planner.build_plan", return_value=_operator_plan()), \
             patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
            failed_body = self.client_.get(reverse("updatecenter:dashboard")).content.decode()
        visible_summary = failed_body.split("<summary>Last update details</summary>", 1)[0]
        self.assertIn("Postflight health check failed", visible_summary)

    def test_no_secrets_leaked_in_rendered_page(self):
        """Not an exhaustive secret scan -- a direct check that this
        page never renders anything read from .env."""
        with FakeRepo() as repo:
            releases_dir = _write_bootstrap_manifest(repo)
            with patch.object(uc_views, "CHECKOUT_ROOT", repo.work), \
                 patch.object(uc_views, "RELEASES_DIRNAME", "deploy/releases"), \
                 patch("updatecenter.views._backend_readiness", return_value=READY_BACKEND):
                resp = self.client_.get(reverse("updatecenter:dashboard"))
                body = resp.content.decode()
                from django.conf import settings
                self.assertNotIn(settings.SECRET_KEY, body)
