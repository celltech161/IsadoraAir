"""Roadmap 2.5D Aircheck start/stop authorization."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import GroupAccess


User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class AircheckControlAuthorizationTests(TestCase):
    def setUp(self):
        reach_group = Group.objects.create(name="Aircheck Reachability Only")
        GroupAccess.objects.create(group=reach_group, allowed_prefixes="/api/aircheck/")
        self.reachable = User.objects.create_user("aircheck_reachable", password="pw")
        self.reachable.groups.add(reach_group)

        capable_group = Group.objects.create(name="Aircheck Control Operators")
        GroupAccess.objects.create(group=capable_group, allowed_prefixes="/api/aircheck/")
        capable_role = Role.objects.create(name="Aircheck Control Operator")
        RoleCapability.objects.create(
            role=capable_role,
            capability=Capability.objects.get(slug="aircheck.control"),
        )
        GroupRole.objects.create(group=capable_group, role=capable_role)
        self.capable = User.objects.create_user("aircheck_capable", password="pw")
        self.capable.groups.add(capable_group)

        self.staff = User.objects.create_user("aircheck_staff", password="pw", is_staff=True)
        self.superuser = User.objects.create_superuser(
            "aircheck_superuser", "aircheck_superuser@example.invalid", "pw"
        )

    @staticmethod
    def _operations():
        return (
            (reverse("aircheck:api-start"), "aircheck.views.start_recording", 500),
            (reverse("aircheck:api-stop"), "aircheck.views.stop_recording", 409),
        )

    def test_unauthenticated_control_is_rejected(self):
        for url, patch_target, _allowed_status in self._operations():
            with self.subTest(url=url), patch(patch_target, return_value=(None, "test")) as service:
                response = self.client.post(url)
                self.assertIn(response.status_code, (302, 401, 403))
                service.assert_not_called()

    def test_reachability_without_capability_does_not_grant_control(self):
        self.client.force_login(self.reachable)
        for url, patch_target, _allowed_status in self._operations():
            with self.subTest(url=url), patch(patch_target, return_value=(None, "test")) as service:
                response = self.client.post(url)
                self.assertEqual(response.status_code, 403)
                service.assert_not_called()

    def test_capable_user_can_cross_both_control_boundaries(self):
        self.client.force_login(self.capable)
        for url, patch_target, allowed_status in self._operations():
            with self.subTest(url=url), patch(patch_target, return_value=(None, "test")) as service:
                response = self.client.post(url)
                self.assertEqual(response.status_code, allowed_status)
                service.assert_called_once_with()

    def test_staff_and_superuser_compatibility_is_retained(self):
        for user in (self.staff, self.superuser):
            self.client.force_login(user)
            for url, patch_target, allowed_status in self._operations():
                with self.subTest(user=user.username, url=url), patch(
                    patch_target, return_value=(None, "test")
                ) as service:
                    response = self.client.post(url)
                    self.assertEqual(response.status_code, allowed_status)
                    service.assert_called_once_with()
            self.client.logout()

    @patch("aircheck.views.current_session", return_value=None)
    @patch("aircheck.views._most_recently_stopped_session", return_value=None)
    @patch("aircheck.views._buffer_dict", return_value={})
    @patch("aircheck.views._recovery_dict", return_value={})
    def test_status_read_remains_capability_free(self, *_mocks):
        self.client.force_login(self.reachable)
        response = self.client.get(reverse("aircheck:api-status"))
        self.assertEqual(response.status_code, 200)
