"""Django Admin configuration contract for 2.17A emergency authority."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from authz.admin import GroupRoleInline, RoleCapabilityInline
from authz.evaluator import authorize
from authz.models import Capability, GroupRole, Role, RoleCapability

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class EmergencyAdminConfigurationTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            "emergency_bootstrap_su", "bootstrap@example.invalid", "pw"
        )
        self.unprivileged_staff = User.objects.create_user(
            "emergency_unprivileged_staff", password="pw", is_staff=True
        )
        self.role = Role.objects.create(name="Designated Emergency Operator")
        self.group = Group.objects.create(name="Designated Emergency Operators")
        self.target = User.objects.create_user("designated_operator", password="pw", is_staff=True)

    def test_superuser_can_reach_every_bootstrap_surface(self):
        client = Client()
        client.force_login(self.superuser)
        urls = (
            reverse("admin:authz_capability_changelist"),
            reverse("admin:authz_role_change", args=[self.role.pk]),
            reverse("admin:auth_group_change", args=[self.group.pk]),
            reverse("admin:auth_user_change", args=[self.target.pk]),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

        capability_page = client.get(urls[0]).content.decode()
        self.assertIn("remote_dj.emergency_mute", capability_page)
        self.assertIn("remote_dj.force_disconnect", capability_page)
        self.assertContains(client.get(urls[1]), "role_capabilities-TOTAL_FORMS")
        self.assertContains(client.get(urls[2]), "role_binding-TOTAL_FORMS")
        self.assertContains(client.get(urls[3]), 'name="groups"')

    def test_registered_admin_exposes_role_group_and_user_assignment_paths(self):
        role_admin = admin.site._registry[Role]
        group_admin = admin.site._registry[Group]
        user_admin = admin.site._registry[User]
        self.assertIn(RoleCapabilityInline, role_admin.inlines)
        self.assertIn(GroupRoleInline, group_admin.inlines)
        permission_fields = next(
            options["fields"] for title, options in user_admin.fieldsets if title == "Permissions"
        )
        self.assertIn("groups", permission_fields)

    def test_superuser_admin_role_inline_grant_and_revoke_take_effect(self):
        GroupRole.objects.create(group=self.group, role=self.role)
        self.target.groups.add(self.group)
        capability = Capability.objects.get(slug="remote_dj.emergency_mute")
        self.assertFalse(authorize(self.target, capability.slug).allowed)

        client = Client()
        client.force_login(self.superuser)
        url = reverse("admin:authz_role_change", args=[self.role.pk])
        management = {
            "role_capabilities-TOTAL_FORMS": "1",
            "role_capabilities-INITIAL_FORMS": "0",
            "role_capabilities-MIN_NUM_FORMS": "0",
            "role_capabilities-MAX_NUM_FORMS": "1000",
        }
        response = client.post(url, {
            "name": self.role.name,
            "description": "",
            "role_capabilities-0-id": "",
            "role_capabilities-0-capability": str(capability.pk),
            "_save": "Save",
            **management,
        })
        self.assertEqual(response.status_code, 302, response.context)
        grant = RoleCapability.objects.get(role=self.role, capability=capability)
        self.assertTrue(authorize(self.target, capability.slug).allowed)

        response = client.post(url, {
            "name": self.role.name,
            "description": "",
            "role_capabilities-0-id": str(grant.pk),
            "role_capabilities-0-capability": str(capability.pk),
            "role_capabilities-0-DELETE": "on",
            "_save": "Save",
            **{**management, "role_capabilities-INITIAL_FORMS": "1"},
        })
        self.assertEqual(response.status_code, 302, response.context)
        self.assertFalse(RoleCapability.objects.filter(pk=grant.pk).exists())
        self.assertFalse(authorize(self.target, capability.slug).allowed)

    def test_staff_without_model_admin_permissions_cannot_self_grant(self):
        client = Client()
        client.force_login(self.unprivileged_staff)
        urls = (
            reverse("admin:authz_capability_changelist"),
            reverse("admin:authz_role_change", args=[self.role.pk]),
            reverse("admin:auth_group_change", args=[self.group.pk]),
            reverse("admin:auth_user_change", args=[self.unprivileged_staff.pk]),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)
                self.assertEqual(client.post(url, {"_save": "Save"}).status_code, 403)

        self.assertFalse(RoleCapability.objects.filter(role=self.role).exists())
        self.assertFalse(GroupRole.objects.filter(group=self.group).exists())
        self.assertFalse(self.unprivileged_staff.groups.exists())

    def test_capabilities_can_be_granted_and_removed_independently(self):
        mute = Capability.objects.get(slug="remote_dj.emergency_mute")
        kick = Capability.objects.get(slug="remote_dj.force_disconnect")
        mute_grant = RoleCapability.objects.create(role=self.role, capability=mute)
        kick_grant = RoleCapability.objects.create(role=self.role, capability=kick)
        GroupRole.objects.create(group=self.group, role=self.role)
        self.target.groups.add(self.group)

        mute_grant.delete()
        self.assertFalse(self.role.capabilities.filter(pk=mute.pk).exists())
        self.assertTrue(self.role.capabilities.filter(pk=kick.pk).exists())
        kick_grant.delete()
        self.assertFalse(self.role.capabilities.filter(pk=kick.pk).exists())
