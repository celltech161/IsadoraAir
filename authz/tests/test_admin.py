"""Roadmap 2.5B admin UX smoke tests -- proves the new admin surfaces
actually render (singleton redirect behavior, User-admin inline,
standalone TalentAssignment admin) rather than just existing as
untested registrations."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment
from monitoring.models import SystemEvent

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ScheduleAccessConfigAdminTests(TestCase):
    def setUp(self):
        self.su = User.objects.create_superuser("admin_su_2_5b", "su@example.invalid", "pw")
        self.client.force_login(self.su)

    def test_changelist_redirects_to_the_singleton_row(self):
        cfg = ScheduleAccessConfig.load()
        resp = self.client.get(reverse("admin:authz_scheduleaccessconfig_changelist"))
        self.assertRedirects(
            resp, reverse("admin:authz_scheduleaccessconfig_change", args=[cfg.pk])
        )

    def test_change_page_renders_and_shows_defaults(self):
        cfg = ScheduleAccessConfig.load()
        resp = self.client.get(reverse("admin:authz_scheduleaccessconfig_change", args=[cfg.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "pre_schedule_allowance_minutes")

    def test_add_is_blocked_once_the_row_exists(self):
        ScheduleAccessConfig.load()
        resp = self.client.get(reverse("admin:authz_scheduleaccessconfig_add"))
        self.assertEqual(resp.status_code, 403)

    def _post_config(self, cfg, *, enabled, pre=10, post=15):
        return self.client.post(
            reverse("admin:authz_scheduleaccessconfig_change", args=[cfg.pk]),
            data={
                "pre_schedule_allowance_minutes": str(pre),
                "post_schedule_allowance_minutes": str(post),
                "scheduled_enforcement_enabled": "on" if enabled else "",
            },
        )

    def test_enabling_with_an_unscheduled_remote_host_is_refused(self):
        role = Role.objects.create(name="Activation Admin Test Role")
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="remote_dj.connect"))
        group = Group.objects.create(name="Activation Admin Test Group")
        GroupRole.objects.create(group=group, role=role)
        dj = User.objects.create_user("activation_admin_dj", password="pw")
        dj.groups.add(group)

        cfg = ScheduleAccessConfig.load()
        resp = self._post_config(cfg, enabled=True)

        self.assertEqual(resp.status_code, 200)  # re-renders the form with an error, not a redirect
        self.assertContains(resp, "activation_admin_dj")
        cfg.refresh_from_db()
        self.assertFalse(cfg.scheduled_enforcement_enabled)

    def test_enabling_after_an_assignment_exists_succeeds(self):
        role = Role.objects.create(name="Activation Admin Test Role 2")
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="remote_dj.connect"))
        group = Group.objects.create(name="Activation Admin Test Group 2")
        GroupRole.objects.create(group=group, role=role)
        dj = User.objects.create_user("activation_admin_dj_2", password="pw")
        dj.groups.add(group)
        TalentAssignment.objects.create(
            user=dj, day_of_week=3, start_time=dt.time(18, 0), end_time=dt.time(20, 0),
        )

        cfg = ScheduleAccessConfig.load()
        resp = self._post_config(cfg, enabled=True)

        self.assertEqual(resp.status_code, 302)
        cfg.refresh_from_db()
        self.assertTrue(cfg.scheduled_enforcement_enabled)

    def test_toggling_enforcement_emits_an_audit_event(self):
        cfg = ScheduleAccessConfig.load()
        before_count = SystemEvent.objects.filter(category="authz").count()

        resp = self._post_config(cfg, enabled=True)
        self.assertEqual(resp.status_code, 302)

        events = SystemEvent.objects.filter(category="authz").order_by("-created_at")
        self.assertEqual(events.count(), before_count + 1)
        self.assertIn("ENABLED", events.first().title)


@override_settings(SECURE_SSL_REDIRECT=False)
class TalentAssignmentAdminTests(TestCase):
    def setUp(self):
        self.su = User.objects.create_superuser("admin_su_ta", "su2@example.invalid", "pw")
        self.client.force_login(self.su)
        self.talent = User.objects.create_user("talent_admin_test", password="pw")

    def test_standalone_changelist_renders(self):
        TalentAssignment.objects.create(
            user=self.talent, day_of_week=2,
            start_time=dt.time(18, 0), end_time=dt.time(20, 0),
        )
        resp = self.client.get(reverse("admin:authz_talentassignment_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "talent_admin_test")

    def test_user_change_page_shows_talent_assignment_inline(self):
        resp = self.client.get(reverse("admin:auth_user_change", args=[self.talent.pk]))
        self.assertEqual(resp.status_code, 200)
        # Formset prefix defaults to the FK's related_name
        # ("talent_assignments" on TalentAssignment.user).
        self.assertContains(resp, "talent_assignments-TOTAL_FORMS")

    def test_add_assignment_via_user_inline(self):
        resp = self.client.post(
            reverse("admin:auth_user_change", args=[self.talent.pk]),
            data=self._user_change_post_data(add_assignment=True),
        )
        # A successful admin POST redirects (302); a validation error
        # re-renders the form (200) -- fail loudly with the page body if
        # that happens, since this is the sort of subtle form-wiring bug
        # that a "assertEqual(200)"-shaped test would otherwise hide.
        if resp.status_code != 302:
            self.fail(f"Admin save did not redirect (status={resp.status_code}): "
                      f"{resp.context['adminform'].form.errors if hasattr(resp, 'context') and resp.context else resp.content[:2000]}")
        self.assertTrue(
            TalentAssignment.objects.filter(user=self.talent, day_of_week=3).exists()
        )

    def _user_change_post_data(self, add_assignment=False):
        data = {
            "username": self.talent.username,
            "first_name": "", "last_name": "", "email": "",
            "is_active": "on",
            "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
            "talent_assignments-TOTAL_FORMS": "1" if add_assignment else "0",
            "talent_assignments-INITIAL_FORMS": "0",
            "talent_assignments-MIN_NUM_FORMS": "0",
            "talent_assignments-MAX_NUM_FORMS": "1000",
        }
        if add_assignment:
            data.update({
                "talent_assignments-0-user": str(self.talent.pk),
                "talent_assignments-0-day_of_week": "3",
                "talent_assignments-0-specific_date": "",
                "talent_assignments-0-start_time": "18:00:00",
                "talent_assignments-0-end_time": "20:00:00",
                "talent_assignments-0-active": "on",
            })
        return data
