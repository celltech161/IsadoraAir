"""Roadmap 2.5C -- safe activation of scheduled enforcement.

Covers the ScheduleAccessConfig.scheduled_enforcement_enabled switch
itself (default OFF, compatibility-vs-enforced behavior at the
authorize() level) and users_missing_talent_assignments_for_scheduled_
capabilities() (the admin activation-safety check). Admin-form-level
validation (refusing to save with enforcement=True while an unscheduled
Remote Host exists) is covered separately in authz.tests.test_admin."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from authz.evaluator import (
    CODE_ALLOWED,
    CODE_CAPABILITY_MISSING,
    CODE_NO_ASSIGNMENT,
    authorize,
    users_missing_talent_assignments_for_scheduled_capabilities,
)
from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment

User = get_user_model()


def make_scheduled_role_and_group(name_suffix):
    role = Role.objects.create(name=f"Activation Test Role {name_suffix}")
    RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="remote_dj.connect"))
    group = Group.objects.create(name=f"Activation Test Group {name_suffix}")
    GroupRole.objects.create(group=group, role=role)
    return role, group


class DefaultActivationStateTests(TestCase):
    def test_fresh_migrate_defaults_enforcement_off(self):
        cfg = ScheduleAccessConfig.load()
        self.assertFalse(cfg.scheduled_enforcement_enabled)


class CompatibilityModeTests(TestCase):
    """scheduled_enforcement_enabled=False (the default) -- 2.5B/2.5C's
    intended legacy/compatibility behavior for existing Remote Hosts."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.role, self.group = make_scheduled_role_and_group("compat")
        self.user = User.objects.create_user("compat_user", password="pw")
        self.user.groups.add(self.group)

    def test_capability_holder_with_no_assignment_is_allowed(self):
        result = authorize(self.user, "remote_dj.connect")
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)

    def test_user_without_capability_is_still_denied(self):
        other = User.objects.create_user("no_cap_compat_user", password="pw")
        result = authorize(other, "remote_dj.connect")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_CAPABILITY_MISSING)

    def test_strict_policy_still_reports_no_assignment_regardless_of_switch(self):
        """The switch changes what a normal (schedule_policy='enforce')
        call site does -- it must NOT change what the evaluator is
        capable of reporting when a caller explicitly asks for the real
        answer."""
        from authz.evaluator import SCHEDULE_POLICY_STRICT
        result = authorize(self.user, "remote_dj.connect", schedule_policy=SCHEDULE_POLICY_STRICT)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_NO_ASSIGNMENT)

    def test_ignore_policy_is_allowed_with_or_without_an_assignment(self):
        from authz.evaluator import SCHEDULE_POLICY_IGNORE
        result = authorize(self.user, "remote_dj.connect", schedule_policy=SCHEDULE_POLICY_IGNORE)
        self.assertTrue(result.allowed)


class EnforcedModeTests(TestCase):
    """scheduled_enforcement_enabled=True."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.save()
        self.role, self.group = make_scheduled_role_and_group("enforced")
        self.user = User.objects.create_user("enforced_user", password="pw")
        self.user.groups.add(self.group)

    def test_capability_with_active_covering_assignment_is_allowed(self):
        import datetime as real_dt
        from zoneinfo import ZoneInfo
        now = real_dt.datetime(2026, 9, 30, 19, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
        TalentAssignment.objects.create(
            user=self.user, day_of_week=now.weekday(),
            start_time=dt.time(18, 0), end_time=dt.time(20, 0), active=True,
        )
        result = authorize(self.user, "remote_dj.connect", now=now)
        self.assertTrue(result.allowed)

    def test_capability_with_no_assignment_is_denied(self):
        result = authorize(self.user, "remote_dj.connect")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_NO_ASSIGNMENT)

    def test_turning_enforcement_back_off_restores_compatibility_without_touching_assignments(self):
        # No assignment exists -- would be denied while enforcement is ON.
        self.assertFalse(authorize(self.user, "remote_dj.connect").allowed)

        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()

        result = authorize(self.user, "remote_dj.connect")
        self.assertTrue(result.allowed)
        self.assertEqual(TalentAssignment.objects.filter(user=self.user).count(), 0)


class BypassCacheTests(TestCase):
    def test_bypass_cache_sees_a_role_capability_added_without_needing_the_normal_signal_invalidated_cache(self):
        role, group = make_scheduled_role_and_group("bypass")
        user = User.objects.create_user("bypass_user", password="pw")
        user.groups.add(group)
        # Warm the normal cache first.
        authorize(user, "remote_dj.connect")

        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="library.view"))
        # The normal (signal-invalidated, same-process) cache already
        # sees this fine too -- bypass_cache's real value is cross-
        # process freshness, not provable from a single Django test
        # process. This just proves the parameter doesn't break anything.
        result = authorize(user, "library.view", bypass_cache=True)
        self.assertTrue(result.allowed)


class MissingAssignmentValidationTests(TestCase):
    def test_no_scheduled_capabilities_at_all_returns_empty(self):
        Capability.objects.filter(requires_schedule=True).update(requires_schedule=False)
        self.assertEqual(users_missing_talent_assignments_for_scheduled_capabilities(), [])

    def test_finds_a_user_with_capability_and_zero_assignments(self):
        role, group = make_scheduled_role_and_group("missing")
        user = User.objects.create_user("missing_assignment_user", password="pw")
        user.groups.add(group)

        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertIn(user, missing)

    def test_finds_a_user_with_only_inactive_assignments(self):
        role, group = make_scheduled_role_and_group("has_row")
        user = User.objects.create_user("has_assignment_row_user", password="pw")
        user.groups.add(group)
        TalentAssignment.objects.create(
            user=user, day_of_week=1, start_time=dt.time(1, 0), end_time=dt.time(2, 0),
            active=False,
        )
        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertIn(user, missing)

    def test_active_future_specific_date_assignment_satisfies_activation_safety(self):
        role, group = make_scheduled_role_and_group("future_specific")
        user = User.objects.create_user("future_specific_assignment_user", password="pw")
        user.groups.add(group)
        TalentAssignment.objects.create(
            user=user,
            specific_date=dt.date(2099, 1, 1),
            start_time=dt.time(1, 0),
            end_time=dt.time(2, 0),
            active=True,
        )

        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertNotIn(user, missing)

    def test_active_recurring_assignment_satisfies_activation_safety(self):
        role, group = make_scheduled_role_and_group("active_recurring")
        user = User.objects.create_user("active_recurring_assignment_user", password="pw")
        user.groups.add(group)
        TalentAssignment.objects.create(
            user=user,
            day_of_week=1,
            start_time=dt.time(1, 0),
            end_time=dt.time(2, 0),
            active=True,
        )

        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertNotIn(user, missing)

    def test_excludes_staff_and_superuser(self):
        role, group = make_scheduled_role_and_group("staffsuper")
        staff = User.objects.create_user("missing_staff_user", password="pw", is_staff=True)
        staff.groups.add(group)
        su = User.objects.create_superuser("missing_su_user", "su@example.invalid", "pw")
        su.groups.add(group)

        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertNotIn(staff, missing)
        self.assertNotIn(su, missing)

    def test_excludes_a_user_whose_role_grants_only_unscheduled_capabilities(self):
        role = Role.objects.create(name="Unscheduled Only Role")
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="library.view"))
        group = Group.objects.create(name="Unscheduled Only Group")
        GroupRole.objects.create(group=group, role=role)
        user = User.objects.create_user("unscheduled_only_user", password="pw")
        user.groups.add(group)

        missing = users_missing_talent_assignments_for_scheduled_capabilities()
        self.assertNotIn(user, missing)
