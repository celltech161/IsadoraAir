"""Roadmap 2.5D listener-counter reset authorization."""
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import GroupAccess


User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ListenerCounterResetAuthorizationTests(TestCase):
    def setUp(self):
        remote_group = Group.objects.get(name="remote_dj")
        self.remote_host = User.objects.create_user("counter_reset_remote_host", password="pw")
        self.remote_host.groups.add(remote_group)

        ops_group = Group.objects.create(name="Listener Counter Operators")
        GroupAccess.objects.create(group=ops_group, allowed_prefixes="/monitoring/")
        ops_role = Role.objects.create(name="Listener Counter Operator")
        RoleCapability.objects.create(
            role=ops_role,
            capability=Capability.objects.get(slug="monitoring.reset_listener_counters"),
        )
        GroupRole.objects.create(group=ops_group, role=ops_role)
        self.capable = User.objects.create_user("counter_reset_capable", password="pw")
        self.capable.groups.add(ops_group)

        self.staff = User.objects.create_user("counter_reset_staff", password="pw", is_staff=True)
        self.superuser = User.objects.create_superuser(
            "counter_reset_superuser", "counter_reset@example.invalid", "pw"
        )

    @staticmethod
    def _urls():
        return (
            reverse("monitoring:api-listener-peak-reset"),
            reverse("monitoring:api-listener-tlh-reset"),
        )

    def _post(self, url):
        with patch("monitoring.views.LISTENER_STATE_PATH", Path("/definitely/not/present")):
            return self.client.post(url)

    def test_unauthenticated_resets_are_rejected(self):
        for url in self._urls():
            with self.subTest(url=url):
                self.assertIn(self._post(url).status_code, (302, 401, 403))

    def test_remote_host_reachability_does_not_grant_reset_authority(self):
        self.client.force_login(self.remote_host)
        for url in self._urls():
            with self.subTest(url=url):
                self.assertEqual(self._post(url).status_code, 403)

    def test_capable_user_can_reset_both_counters(self):
        self.client.force_login(self.capable)
        for url in self._urls():
            with self.subTest(url=url):
                self.assertEqual(self._post(url).status_code, 200)

    def test_staff_and_superuser_compatibility_is_retained(self):
        for user in (self.staff, self.superuser):
            self.client.force_login(user)
            for url in self._urls():
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self._post(url).status_code, 200)
            self.client.logout()
