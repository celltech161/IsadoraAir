"""Roadmap 2.5A security fix: monitoring/views.py::api_restart_check
previously restarted a real systemd unit (through the root-owned
protected broker) for ANY authenticated account whose GroupAccess
reachability happened to include /monitoring/ -- notably `remote_dj`,
whose seeded GroupAccess row grants that prefix for dashboard viewing,
not service control. There was no other check in the view at all.

This suite proves the specific property the roadmap 2.5 audit exists to
establish: reaching a URL via GroupAccess must never, by itself, imply
authorization to perform the operation exposed there."""
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import GroupAccess
from monitoring.models import MonitorCheck


def make_systemd_check(name="isadoraair-engine.service"):
    return MonitorCheck.objects.create(
        name=name, kind="systemd", systemd_unit=name, enabled=True,
    )


@override_settings(SECURE_SSL_REDIRECT=False)
class RestartCheckAuthorizationTests(TestCase):
    def setUp(self):
        self.check = make_systemd_check()
        self.url = reverse("monitoring:api-restart-check", args=[self.check.pk])

    def _post(self):
        return self.client.post(self.url)

    def test_anonymous_is_redirected_to_login_not_allowed_through(self):
        resp = self._post()
        self.assertNotEqual(resp.status_code, 202)

    def test_plain_authenticated_user_with_no_recognized_group_is_never_let_through(self):
        """No GroupAccess row at all -- GroupBasedAccessMiddleware itself
        already blocks this (redirect, since this path doesn't start
        with /api/ or /ws/), before authorize() is ever reached. Not
        this suite's defect to prove, but asserting it never reaches
        202 keeps this test meaningful if that middleware behavior ever
        changes."""
        user = User.objects.create_user("plainuser", "plain@example.invalid", "pw")
        self.client.force_login(user)
        resp = self._post()
        self.assertNotEqual(resp.status_code, 202)

    def test_remote_dj_account_cannot_restart_a_service(self):
        """The exact defect: remote_dj's GroupAccess grants /monitoring/
        reachability for dashboard viewing, but that must never imply
        the monitoring.restart_service capability."""
        dj = User.objects.create_user("dj_restart_test", "dj@example.invalid", "pw")
        group, _ = Group.objects.get_or_create(name="remote_dj")
        dj.groups.add(group)
        self.client.force_login(dj)

        with patch("monitoring.views.UpdaterClient") as mock_client_cls:
            resp = self._post()

        self.assertEqual(resp.status_code, 403)
        mock_client_cls.assert_not_called()

    def test_user_with_monitoring_restart_service_capability_can_restart(self):
        group = Group.objects.create(name="Ops Test Group")
        role = Role.objects.create(name="Ops Test Role")
        cap = Capability.objects.get(slug="monitoring.restart_service")
        RoleCapability.objects.create(role=role, capability=cap)
        GroupRole.objects.create(group=group, role=role)
        # GroupAccess (reachability) is a SEPARATE grant from GroupRole
        # (capability) -- this group needs both to actually reach and
        # use the endpoint, proving the two systems compose rather than
        # either one alone being sufficient.
        GroupAccess.objects.create(group=group, allowed_prefixes="/monitoring/")

        user = User.objects.create_user("ops_test_user", "ops@example.invalid", "pw")
        user.groups.add(group)
        self.client.force_login(user)

        mock_instance = patch("monitoring.views.UpdaterClient").start()
        self.addCleanup(patch.stopall)
        mock_instance.return_value.restart_operator_service.return_value = {
            "operation_id": "op-123", "state": "requested",
        }

        resp = self._post()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["operation_id"], "op-123")
        mock_instance.return_value.restart_operator_service.assert_called_once_with(
            self.check.systemd_unit
        )

    def test_superuser_can_restart_without_any_role(self):
        su = User.objects.create_superuser("restart_su", "su@example.invalid", "pw")
        self.client.force_login(su)

        with patch("monitoring.views.UpdaterClient") as mock_client_cls:
            mock_client_cls.return_value.restart_operator_service.return_value = {
                "operation_id": "op-su", "state": "requested",
            }
            resp = self._post()
        self.assertEqual(resp.status_code, 202)

    def test_staff_can_restart_without_any_role(self):
        """Staff compatibility: preserves today's actual behavior (staff
        bypasses GroupBasedAccessMiddleware entirely, so a staff account
        could always reach and use this endpoint) without requiring a
        Role backfill."""
        staff = User.objects.create_user("restart_staff", "staff@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)

        with patch("monitoring.views.UpdaterClient") as mock_client_cls:
            mock_client_cls.return_value.restart_operator_service.return_value = {
                "operation_id": "op-staff", "state": "requested",
            }
            resp = self._post()
        self.assertEqual(resp.status_code, 202)
