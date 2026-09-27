"""Roadmap 2.5D authorization closeout for schedule/definition/log mutations.

The requests intentionally use invalid JSON or nonexistent object IDs after
the authorization boundary.  A capable caller therefore reaches the ordinary
400/404/benign-empty outcome without changing fixture data, while a reachable
caller lacking the capability must be stopped at 403 before parsing or lookup.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import GroupAccess


User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class DefinitionAndLogMutationAuthorizationTests(TestCase):
    def setUp(self):
        self.reach_group = Group.objects.create(name="2.5D Reachability Only")
        GroupAccess.objects.create(group=self.reach_group, allowed_prefixes="/api/")
        self.reachable = User.objects.create_user("2_5d_reachable", password="pw")
        self.reachable.groups.add(self.reach_group)

        capable_group = Group.objects.create(name="2.5D Schedule Definition Editors")
        GroupAccess.objects.create(group=capable_group, allowed_prefixes="/api/")
        role = Role.objects.create(name="2.5D Schedule Definition Editor")
        for slug in ("schedule.edit", "rotations_playlists.edit"):
            RoleCapability.objects.create(
                role=role, capability=Capability.objects.get(slug=slug)
            )
        GroupRole.objects.create(group=capable_group, role=role)
        self.capable = User.objects.create_user("2_5d_capable", password="pw")
        self.capable.groups.add(capable_group)

        self.staff = User.objects.create_user("2_5d_staff", password="pw", is_staff=True)
        self.superuser = User.objects.create_superuser(
            "2_5d_superuser", "2_5d_superuser@example.invalid", "pw"
        )

    @staticmethod
    def _schedule_mutations():
        return (
            ("schedule create/update", "POST", reverse("library:api-schedule-list")),
            ("schedule delete", "DELETE", reverse("library:api-schedule-delete", args=[999999])),
        )

    @staticmethod
    def _rotation_mutations():
        return (
            ("rotation create", "POST", reverse("library:api-rotation-list")),
            ("rotation update", "PATCH", reverse("library:api-rotation-detail", args=[999999])),
            ("rotation delete", "DELETE", reverse("library:api-rotation-detail", args=[999999])),
            ("rotation add slot", "POST", reverse("library:api-rotation-add-slot", args=[999999])),
            ("rotation remove slot", "DELETE", reverse("library:api-rotation-remove-slot", args=[999999])),
            ("rotation reorder", "POST", reverse("library:api-rotation-reorder", args=[999999])),
            ("rotation copy", "POST", reverse("library:api-rotation-copy", args=[999999])),
        )

    @staticmethod
    def _playlist_mutations():
        return (
            ("playlist create", "POST", reverse("library:api-playlist-list")),
            ("playlist update", "PATCH", reverse("library:api-playlist-detail", args=[999999])),
            ("playlist delete", "DELETE", reverse("library:api-playlist-detail", args=[999999])),
            ("playlist add item", "POST", reverse("library:api-playlist-add-item", args=[999999])),
            ("playlist remove item", "DELETE", reverse("library:api-playlist-remove-item", args=[999999])),
            ("playlist reorder", "POST", reverse("library:api-playlist-reorder", args=[999999])),
            ("playlist copy", "POST", reverse("library:api-playlist-copy", args=[999999])),
        )

    @staticmethod
    def _playlist_log_mutations():
        return (
            ("log build", "POST", reverse("library:api-log-build")),
            ("log update", "PATCH", reverse("library:api-log-update", args=[999999])),
            ("log delete", "DELETE", reverse("library:api-log-delete", args=[999999])),
            ("log item swap", "PATCH", reverse("library:api-log-item-swap", args=[999999])),
            ("log reorder", "POST", reverse("library:api-log-reorder", args=[999999])),
        )

    @classmethod
    def _all_mutations(cls):
        return (
            cls._schedule_mutations()
            + cls._rotation_mutations()
            + cls._playlist_mutations()
            + cls._playlist_log_mutations()
        )

    def _request(self, method, url):
        return self.client.generic(method, url, data=b"{", content_type="application/json")

    def test_unauthenticated_mutations_are_rejected(self):
        for label, method, url in self._all_mutations():
            with self.subTest(endpoint=label):
                response = self._request(method, url)
                self.assertIn(response.status_code, (302, 401, 403))

    def test_reachability_without_capability_does_not_authorize_mutation(self):
        self.client.force_login(self.reachable)
        for label, method, url in self._all_mutations():
            with self.subTest(endpoint=label):
                self.assertEqual(self._request(method, url).status_code, 403)

    def test_correct_capabilities_allow_every_mutation_boundary(self):
        self.client.force_login(self.capable)
        for label, method, url in self._all_mutations():
            with self.subTest(endpoint=label):
                self.assertNotEqual(self._request(method, url).status_code, 403)

    def test_staff_and_superuser_compatibility_is_retained(self):
        for user in (self.staff, self.superuser):
            self.client.force_login(user)
            for label, method, url in self._all_mutations():
                with self.subTest(user=user.username, endpoint=label):
                    self.assertNotEqual(self._request(method, url).status_code, 403)
            self.client.logout()

    def test_schedule_rotation_playlist_and_log_reads_remain_capability_free(self):
        self.client.force_login(self.reachable)
        read_requests = (
            ("schedule list", "GET", reverse("library:api-schedule-list")),
            ("rotation list", "GET", reverse("library:api-rotation-list")),
            ("playlist list", "GET", reverse("library:api-playlist-list")),
            ("log list", "GET", reverse("library:api-log-list-date", args=["2026-09-27"])),
            ("log get", "GET", reverse("library:api-log-get", args=["2026-09-27", 12])),
            ("log preview dry-run", "POST", reverse("library:api-log-preview")),
        )
        for label, method, url in read_requests:
            with self.subTest(endpoint=label):
                self.assertNotEqual(self._request(method, url).status_code, 403)
