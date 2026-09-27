"""Roadmap 2.5A compatibility proof: adding the authz app (Role/
Capability/RoleCapability/GroupRole) and binding GroupRole rows to the
existing Contributor/remote_dj groups must not change GroupAccess-driven
coarse reachability AT ALL -- the two systems are independent by design
(see authz/models.py's module docstring). This file existed as a gap in
the pre-2.5 test suite (no dedicated GroupAccess/middleware test file
was found during the roadmap 2.5 audit); it's added now specifically to
prove the compatibility property the roadmap requires, not to newly
test GroupAccess/GroupBasedAccessMiddleware's pre-existing behavior in
general."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import GroupRole

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ExistingGroupReachabilityUnchangedTests(TestCase):
    def setUp(self):
        self.contributor = User.objects.create_user("contrib_compat", "cc@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        self.remote_dj = User.objects.create_user("dj_compat", "dc@example.invalid", "pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_dj.groups.add(dj_group)

        # Both groups' seed migration (authz.0002) should have created a
        # GroupRole binding for each -- pin that as a precondition so a
        # failure in the reachability assertions below is unambiguous.
        self.assertTrue(GroupRole.objects.filter(group=contrib_group).exists())
        self.assertTrue(GroupRole.objects.filter(group=dj_group).exists())

    def test_contributor_reaches_library_page(self):
        self.client.force_login(self.contributor)
        resp = self.client.get("/library/")
        self.assertEqual(resp.status_code, 200)

    def test_contributor_reaches_track_api_reads(self):
        self.client.force_login(self.contributor)
        resp = self.client.get(reverse("library:api-track-list"))
        self.assertEqual(resp.status_code, 200)

    def test_contributor_is_blocked_from_reports(self):
        self.client.force_login(self.contributor)
        resp = self.client.get("/reports/")
        self.assertNotEqual(resp.status_code, 200)

    def test_contributor_is_blocked_from_schedule_api(self):
        self.client.force_login(self.contributor)
        resp = self.client.get(reverse("library:api-schedule-list"))
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_reaches_remote_dj_page(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.get("/remote-dj/")
        self.assertEqual(resp.status_code, 200)

    def test_remote_dj_reaches_monitoring_dashboard(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.get("/monitoring/")
        self.assertEqual(resp.status_code, 200)

    def test_remote_dj_is_blocked_from_reports(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.get("/reports/")
        self.assertNotEqual(resp.status_code, 200)

    def test_unrecognized_authenticated_user_redirected_to_welcome(self):
        plain = User.objects.create_user("plain_compat", "pc@example.invalid", "pw")
        self.client.force_login(plain)
        resp = self.client.get("/library/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/welcome/", resp.url)

    def test_staff_bypasses_group_access_entirely(self):
        staff = User.objects.create_user("staff_compat", "sc@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get("/reports/")
        self.assertEqual(resp.status_code, 200)
