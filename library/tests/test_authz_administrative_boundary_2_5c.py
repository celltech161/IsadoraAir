"""Roadmap 2.5C -- proves again, at real endpoints (not just the
evaluator unit level covered by authz.tests.test_evaluator's
test_ordinary_talent_capability_does_not_grant_administrative_authority),
that even the most powerful REAL, seeded talent Role (Remote Host) never
implies Django Admin, Update Center, or Web Requests configuration
authority. Uses the actual seeded Remote Host Role/remote_dj Group
(migrations authz.0002/0006), not a synthetic all-capabilities test
Role, to prove this against what a real station would actually be
running."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class AdministrativeBoundaryTests(TestCase):
    def setUp(self):
        self.remote_host = User.objects.create_user("admin_boundary_remote_host", password="pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_host.groups.add(dj_group)
        self.client.force_login(self.remote_host)

    def test_remote_host_cannot_reach_django_admin(self):
        resp = self.client.get("/admin/")
        self.assertNotEqual(resp.status_code, 200)

    def test_remote_host_cannot_reach_update_center_dashboard(self):
        resp = self.client.get(reverse("updatecenter:dashboard"))
        self.assertNotEqual(resp.status_code, 200)

    def test_remote_host_cannot_start_an_update(self):
        resp = self.client.post(reverse("updatecenter:start-update"))
        self.assertNotEqual(resp.status_code, 200)

    def test_remote_host_cannot_reach_web_requests_config(self):
        resp = self.client.get("/api/web-request/config/")
        self.assertNotEqual(resp.status_code, 200)

    def test_remote_host_is_not_staff_or_superuser(self):
        self.remote_host.refresh_from_db()
        self.assertFalse(self.remote_host.is_staff)
        self.assertFalse(self.remote_host.is_superuser)

    def test_remote_host_cannot_reach_reports(self):
        resp = self.client.get("/reports/")
        self.assertNotEqual(resp.status_code, 200)
