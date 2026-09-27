"""Roadmap 2.5A -- the central evaluator (authz.evaluator.authorize) in
isolation, independent of any particular view. See
library/tests/test_authz_category_track_enforcement.py and
monitoring/tests/test_authz_restart_service.py for the endpoint-level
security-fix proofs; this file is about the evaluator's own contract."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import TestCase

from authz.evaluator import (
    CODE_ALLOWED,
    CODE_CAPABILITY_MISSING,
    CODE_DISABLED_ACCOUNT,
    CODE_UNAUTHENTICATED,
    authorize,
)
from authz.models import Capability, GroupRole, Role, RoleCapability

User = get_user_model()


def make_role(name, slugs):
    role = Role.objects.create(name=name)
    for slug in slugs:
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug=slug))
    return role


def make_group_with_role(group_name, role):
    group = Group.objects.create(name=group_name)
    GroupRole.objects.create(group=group, role=role)
    return group


class CapabilityCompositionTests(TestCase):
    def test_single_role_grants_its_capabilities(self):
        role = make_role("Single Role Test", ["reports.view"])
        group = make_group_with_role("Single Role Test Group", role)
        user = User.objects.create_user("u1", "u1@example.invalid", "pw")
        user.groups.add(group)

        result = authorize(user, "reports.view")
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)

    def test_missing_capability_is_denied(self):
        role = make_role("Missing Cap Test", ["reports.view"])
        group = make_group_with_role("Missing Cap Test Group", role)
        user = User.objects.create_user("u2", "u2@example.invalid", "pw")
        user.groups.add(group)

        result = authorize(user, "system.administer")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_CAPABILITY_MISSING)

    def test_multiple_groups_union_correctly(self):
        role_a = make_role("Union Role A", ["reports.view"])
        role_b = make_role("Union Role B", ["fx.manage_carts"])
        group_a = make_group_with_role("Union Group A", role_a)
        group_b = make_group_with_role("Union Group B", role_b)
        user = User.objects.create_user("u3", "u3@example.invalid", "pw")
        user.groups.add(group_a, group_b)

        self.assertTrue(authorize(user, "reports.view").allowed)
        self.assertTrue(authorize(user, "fx.manage_carts").allowed)
        self.assertFalse(authorize(user, "system.administer").allowed)

    def test_two_roles_on_the_same_group_chain_is_not_possible_but_two_groups_is(self):
        # A Group binds to exactly one Role (GroupRole.group is
        # OneToOneField) -- multi-role composition happens by a USER
        # belonging to multiple Groups, each bound to a different Role
        # (covered above). This test just documents/pins that
        # constraint so a future change to it is a deliberate decision.
        role = make_role("OneToOne Pin Role", ["reports.view"])
        group = make_group_with_role("OneToOne Pin Group", role)
        other_role = Role.objects.create(name="OneToOne Pin Role 2")
        with self.assertRaises(Exception):
            GroupRole.objects.create(group=group, role=other_role)

    def test_unauthenticated_user_is_denied(self):
        result = authorize(AnonymousUser(), "reports.view")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_UNAUTHENTICATED)

    def test_none_user_is_denied(self):
        result = authorize(None, "reports.view")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_UNAUTHENTICATED)

    def test_inactive_user_is_denied_even_with_a_granting_role(self):
        role = make_role("Inactive Test Role", ["reports.view"])
        group = make_group_with_role("Inactive Test Group", role)
        user = User.objects.create_user("u4", "u4@example.invalid", "pw", is_active=False)
        user.groups.add(group)

        result = authorize(user, "reports.view")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_DISABLED_ACCOUNT)

    def test_superuser_is_authorized_without_any_role(self):
        su = User.objects.create_superuser("su1", "su1@example.invalid", "pw")
        result = authorize(su, "system.administer")
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)

    def test_staff_is_authorized_without_any_role(self):
        staff = User.objects.create_user("staff1", "staff1@example.invalid", "pw", is_staff=True)
        result = authorize(staff, "monitoring.restart_service")
        self.assertTrue(result.allowed)

    def test_ordinary_talent_capability_does_not_grant_administrative_authority(self):
        """The one-way implication from PROJECT_NOTES.md's "Roadmap 2.5"
        section: holding e.g. remote_dj.connect must never make
        is_staff/is_superuser true, and must never satisfy
        system.administer."""
        role = make_role("Talent Only Role", ["remote_dj.connect", "playout.control"])
        group = make_group_with_role("Talent Only Group", role)
        user = User.objects.create_user("talent1", "talent1@example.invalid", "pw")
        user.groups.add(group)

        self.assertTrue(authorize(user, "remote_dj.connect").allowed)
        self.assertFalse(authorize(user, "system.administer").allowed)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_unknown_capability_slug_raises(self):
        user = User.objects.create_user("u5", "u5@example.invalid", "pw")
        with self.assertRaises(ValueError):
            authorize(user, "this.slug.does.not.exist")

    def test_cache_invalidates_when_a_capability_is_added_to_a_role(self):
        role = make_role("Cache Test Role", ["reports.view"])
        group = make_group_with_role("Cache Test Group", role)
        user = User.objects.create_user("u6", "u6@example.invalid", "pw")
        user.groups.add(group)

        self.assertFalse(authorize(user, "fx.manage_carts").allowed)

        RoleCapability.objects.create(
            role=role, capability=Capability.objects.get(slug="fx.manage_carts")
        )

        self.assertTrue(authorize(user, "fx.manage_carts").allowed)

    def test_cache_invalidates_when_group_role_binding_is_removed(self):
        role = make_role("Revoke Test Role", ["reports.view"])
        group = make_group_with_role("Revoke Test Group", role)
        user = User.objects.create_user("u7", "u7@example.invalid", "pw")
        user.groups.add(group)

        self.assertTrue(authorize(user, "reports.view").allowed)

        GroupRole.objects.filter(group=group).delete()

        self.assertFalse(authorize(user, "reports.view").allowed)
