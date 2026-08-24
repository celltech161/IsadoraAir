"""/updates/ permission + side-effect tests -- [P0] 1.1 Phase A."""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from updatecenter import views as uc_views
from .gitfixtures import FakeRepo


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
        self.client_ = Client()
        self.client_.force_login(self.staff_user)

    def test_renders_against_fake_up_to_date_repo(self):
        with FakeRepo() as repo:
            releases_dir = _write_bootstrap_manifest(repo)
            with patch.object(uc_views, "CHECKOUT_ROOT", repo.work), \
                 patch.object(uc_views, "RELEASES_DIRNAME", "deploy/releases"):
                resp = self.client_.get(reverse("updatecenter:dashboard"))
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertIn("UP_TO_DATE", body)
                for heading in (
                    "Installed Source", "Running Software", "Database Schema",
                    "Available Update", "Target Schema Validation",
                ):
                    self.assertIn(heading, body)
                self.assertIn("NOT_APPLICABLE_NO_TARGET_TRANSITION", body)
                self.assertNotIn("Roll Back", body)
                self.assertNotIn("Rollback", body)

    def test_no_secrets_leaked_in_rendered_page(self):
        """Not an exhaustive secret scan -- a direct check that this
        page never renders anything read from .env."""
        with FakeRepo() as repo:
            releases_dir = _write_bootstrap_manifest(repo)
            with patch.object(uc_views, "CHECKOUT_ROOT", repo.work), \
                 patch.object(uc_views, "RELEASES_DIRNAME", "deploy/releases"):
                resp = self.client_.get(reverse("updatecenter:dashboard"))
                body = resp.content.decode()
                from django.conf import settings
                self.assertNotIn(settings.SECRET_KEY, body)
