"""Roadmap 2.5B -- scheduled-window evaluation.

Every test constructs an explicit aware datetime and passes it to
authorize(..., now=...) rather than relying on the real wall clock or
any time-freezing library (none is a project dependency) -- this is also
exactly how a future 2.5C caller with its own authoritative instant
(e.g. a signaling server timestamp) would use the evaluator.

Capability fixture used throughout: `remote_dj.connect`
(requires_schedule=True, seeded by authz.migrations.0002) granted via a
dedicated test Role/Group, kept separate from the real seeded "Remote
Host" Role so a test's Role edits can never affect another test.

Roadmap 2.5C note: every check here uses schedule_policy="strict" (via
the `check()` helper below) so this file keeps testing the scheduling
MECHANISM itself, independent of ScheduleAccessConfig.
scheduled_enforcement_enabled (default OFF as of 2.5C -- see
authz.tests.test_scheduled_enforcement_activation for coverage of the
station-switch/compatibility-policy behavior that flag controls)."""
import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from authz.evaluator import (
    CODE_ALLOWED,
    CODE_CAPABILITY_MISSING,
    CODE_INACTIVE_ASSIGNMENT,
    CODE_NO_ASSIGNMENT,
    CODE_OUTSIDE_SCHEDULE_WINDOW,
    SCHEDULE_POLICY_STRICT,
    authorize,
)
from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment
from library.models import StationTimeConfig

User = get_user_model()

STATION_TZ = "America/Chicago"


def station_dt(year, month, day, hour, minute, second=0, tz=STATION_TZ):
    """An aware datetime already expressed in station-local wall-clock
    terms, for readability in test bodies -- authorize()'s `now` accepts
    any aware datetime and converts it internally, so passing it
    pre-localized to the station zone is just the clearest way to write
    "17:49:59 station time" as a test fixture."""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(tz))


class ScheduledEvaluatorTestCase(TestCase):
    """Common fixture: a user granted remote_dj.connect through a
    dedicated test Role/Group, station timezone pinned to Chicago
    (matches settings.TIME_ZONE, so tests not specifically about
    cross-timezone behavior don't need to think about offsets)."""

    def setUp(self):
        StationTimeConfig.objects.update_or_create(pk=1, defaults={"timezone": STATION_TZ})

        role = Role.objects.create(name=f"Scheduled Test Role {id(self)}")
        RoleCapability.objects.create(
            role=role, capability=Capability.objects.get(slug="remote_dj.connect")
        )
        group = Group.objects.create(name=f"Scheduled Test Group {id(self)}")
        GroupRole.objects.create(group=group, role=role)

        self.user = User.objects.create_user(f"scheduled_user_{id(self)}", password="pw")
        self.user.groups.add(group)

    def set_allowances(self, pre, post):
        ScheduleAccessConfig.objects.update_or_create(
            pk=1,
            defaults={
                "pre_schedule_allowance_minutes": pre,
                "post_schedule_allowance_minutes": post,
            },
        )

    def assign(self, **kwargs):
        kwargs.setdefault("user", self.user)
        kwargs.setdefault("active", True)
        return TalentAssignment.objects.create(**kwargs)

    def check(self, when):
        return authorize(self.user, "remote_dj.connect", now=when, schedule_policy=SCHEDULE_POLICY_STRICT)


class OrdinaryAssignmentWindowTests(ScheduledEvaluatorTestCase):
    """schedule 18:00-20:00, pre=10, post=15 -> [17:50:00, 20:15:00)."""

    def setUp(self):
        super().setUp()
        self.set_allowances(pre=10, post=15)
        # A Wednesday, arbitrarily -- day_of_week=2.
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0))

    def test_17_49_59_denied(self):
        result = self.check(station_dt(2026, 9, 30, 17, 49, 59))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)

    def test_17_50_00_allowed(self):
        result = self.check(station_dt(2026, 9, 30, 17, 50, 0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)

    def test_during_show_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 30, 19, 0, 0)).allowed)

    def test_20_00_00_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 30, 20, 0, 0)).allowed)

    def test_20_14_59_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 30, 20, 14, 59)).allowed)

    def test_20_15_00_denied(self):
        result = self.check(station_dt(2026, 9, 30, 20, 15, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)


class ZeroAllowanceTests(ScheduledEvaluatorTestCase):
    def setUp(self):
        super().setUp()
        self.set_allowances(pre=0, post=0)
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0))

    def test_immediately_before_start_denied(self):
        self.assertFalse(self.check(station_dt(2026, 9, 30, 17, 59, 59)).allowed)

    def test_exact_start_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 30, 18, 0, 0)).allowed)

    def test_immediately_before_end_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 30, 19, 59, 59)).allowed)

    def test_exact_end_denied(self):
        self.assertFalse(self.check(station_dt(2026, 9, 30, 20, 0, 0)).allowed)


class RoleCapabilitySeparationTests(ScheduledEvaluatorTestCase):
    def setUp(self):
        super().setUp()
        self.set_allowances(pre=10, post=15)

    def test_capability_and_assignment_allowed(self):
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        self.assertTrue(self.check(station_dt(2026, 9, 30, 19, 0, 0)).allowed)

    def test_capability_and_no_assignment_denied(self):
        result = self.check(station_dt(2026, 9, 30, 19, 0, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_NO_ASSIGNMENT)

    def test_assignment_and_no_capability_denied(self):
        other_user = User.objects.create_user("no_capability_user", password="pw")
        TalentAssignment.objects.create(
            user=other_user, day_of_week=2,
            start_time=dt.time(18, 0), end_time=dt.time(20, 0), active=True,
        )
        result = authorize(other_user, "remote_dj.connect", now=station_dt(2026, 9, 30, 19, 0, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_CAPABILITY_MISSING)

    def test_unscheduled_capability_never_needs_an_assignment(self):
        # This user has the SAME role/group as self.user but we ask for
        # an unscheduled capability instead -- must not even look at
        # TalentAssignment.
        RoleCapability.objects.create(
            role=Role.objects.get(group_bindings__group__in=self.user.groups.all()),
            capability=Capability.objects.get(slug="library.view"),
        )
        result = authorize(self.user, "library.view", now=station_dt(2026, 9, 30, 3, 0, 0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)


class RecurringAndDateApplicabilityTests(ScheduledEvaluatorTestCase):
    def setUp(self):
        super().setUp()
        self.set_allowances(pre=0, post=0)

    def test_recurring_weekday_matches(self):
        # 2026-09-30 is a Wednesday (day_of_week=2).
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        self.assertTrue(self.check(station_dt(2026, 9, 30, 19, 0, 0)).allowed)

    def test_wrong_weekday_denied(self):
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        # 2026-10-01 is a Thursday.
        result = self.check(station_dt(2026, 10, 1, 19, 0, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)

    def test_specific_date_matches(self):
        self.assign(specific_date=dt.date(2026, 12, 25), start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        self.assertTrue(self.check(station_dt(2026, 12, 25, 19, 0, 0)).allowed)

    def test_wrong_specific_date_denied(self):
        self.assign(specific_date=dt.date(2026, 12, 25), start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        result = self.check(station_dt(2026, 12, 26, 19, 0, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)

    def test_inactive_assignment_that_would_otherwise_match_is_denied_with_its_own_code(self):
        self.assign(day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0), active=False)
        result = self.check(station_dt(2026, 9, 30, 19, 0, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_INACTIVE_ASSIGNMENT)


class CrossMidnightRecurringTests(ScheduledEvaluatorTestCase):
    """Saturday 22:00 -> 02:00 (Sunday), pre=10, post=15 ->
    effective [Sat 21:50:00, Sun 02:15:00). 2026-10-03 is a Saturday."""

    def setUp(self):
        super().setUp()
        self.set_allowances(pre=10, post=15)
        self.assign(day_of_week=5, start_time=dt.time(22, 0), end_time=dt.time(2, 0))

    def test_saturday_before_effective_start_denied(self):
        result = self.check(station_dt(2026, 10, 3, 21, 49, 59))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)

    def test_saturday_during_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 10, 3, 23, 0, 0)).allowed)

    def test_sunday_00_30_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 10, 4, 0, 30, 0)).allowed)

    def test_sunday_immediately_before_effective_end_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 10, 4, 2, 14, 59)).allowed)

    def test_effective_end_exactly_denied(self):
        result = self.check(station_dt(2026, 10, 4, 2, 15, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)


class CrossMidnightSpecificDateTests(ScheduledEvaluatorTestCase):
    """The same overnight shape as above, but as a one-off specific-date
    override anchored on 2026-09-26 (a Saturday), same as the workorder's
    own example date."""

    def setUp(self):
        super().setUp()
        self.set_allowances(pre=10, post=15)
        self.assign(specific_date=dt.date(2026, 9, 26), start_time=dt.time(22, 0), end_time=dt.time(2, 0))

    def test_before_effective_start_denied(self):
        self.assertFalse(self.check(station_dt(2026, 9, 26, 21, 49, 59)).allowed)

    def test_during_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 26, 23, 30, 0)).allowed)

    def test_after_midnight_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 27, 0, 45, 0)).allowed)

    def test_immediately_before_effective_end_allowed(self):
        self.assertTrue(self.check(station_dt(2026, 9, 27, 2, 14, 59)).allowed)

    def test_effective_end_exactly_denied(self):
        self.assertFalse(self.check(station_dt(2026, 9, 27, 2, 15, 0)).allowed)

    def test_the_following_days_wall_clock_time_alone_does_not_match(self):
        """2026-09-27 at 23:30 is nowhere near this assignment's window --
        pins that the specific_date anchor doesn't leak into a second,
        unrelated day."""
        result = self.check(station_dt(2026, 9, 27, 23, 30, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)


class AllowanceCrossesCalendarBoundaryTests(ScheduledEvaluatorTestCase):
    """The workorder's own boundary-crossing example: a same-day (NOT
    end<=start) assignment whose PRE allowance alone pushes its effective
    start into the previous calendar day, and the mirror case for POST
    pushing the effective end into the next calendar day."""

    def test_pre_allowance_reaches_back_into_previous_day(self):
        # Recurring Saturday 00:05-02:00, pre=15 -> Friday 23:50 is
        # already authorized. 2026-10-03 is a Saturday, so its Friday is
        # 2026-10-02.
        self.set_allowances(pre=15, post=0)
        self.assign(day_of_week=5, start_time=dt.time(0, 5), end_time=dt.time(2, 0))
        self.assertFalse(self.check(station_dt(2026, 10, 2, 23, 49, 59)).allowed)
        self.assertTrue(self.check(station_dt(2026, 10, 2, 23, 50, 0)).allowed)
        self.assertTrue(self.check(station_dt(2026, 10, 3, 0, 5, 0)).allowed)

    def test_pre_allowance_specific_date_reaches_back_into_previous_day(self):
        # specific date 2026-09-27, start 00:05, pre=15 -> authorized
        # starting 2026-09-26 23:50 (workorder's own example).
        self.set_allowances(pre=15, post=0)
        self.assign(specific_date=dt.date(2026, 9, 27), start_time=dt.time(0, 5), end_time=dt.time(2, 0))
        self.assertFalse(self.check(station_dt(2026, 9, 26, 23, 49, 59)).allowed)
        self.assertTrue(self.check(station_dt(2026, 9, 26, 23, 55, 0)).allowed)

    def test_post_allowance_reaches_forward_into_next_day(self):
        # Recurring Wednesday 22:00-23:50 (same-day, does NOT cross
        # midnight on its own), post=15 -> authorized through
        # Thursday 00:04:59.
        self.set_allowances(pre=0, post=15)
        self.assign(day_of_week=2, start_time=dt.time(22, 0), end_time=dt.time(23, 50))
        self.assertTrue(self.check(station_dt(2026, 10, 1, 0, 4, 59)).allowed)
        self.assertFalse(self.check(station_dt(2026, 10, 1, 0, 5, 0)).allowed)


class StationTimezoneAuthorityTests(ScheduledEvaluatorTestCase):
    """Pins the station timezone to something clearly different from
    settings.TIME_ZONE (America/Chicago) and from UTC, and proves
    evaluation follows the CONFIGURED station timezone -- not the
    process/Django-settings default, and not a naive/UTC reading of the
    same instant."""

    def test_evaluation_follows_configured_station_timezone_not_settings_default(self):
        StationTimeConfig.objects.update_or_create(pk=1, defaults={"timezone": "Pacific/Honolulu"})
        self.set_allowances(pre=0, post=0)
        # Recurring Wednesday 09:00-10:00 HONOLULU time.
        self.assign(day_of_week=2, start_time=dt.time(9, 0), end_time=dt.time(10, 0))

        # 2026-09-30 14:30 UTC == 2026-09-30 04:30 Honolulu (UTC-10, no
        # DST) == 2026-09-30 09:30 America/Chicago (UTC-5 in September).
        # If the evaluator wrongly used America/Chicago (settings.TIME_ZONE)
        # instead of the configured station timezone, this instant would
        # incorrectly be authorized (09:30 Chicago falls inside 09:00-10:00);
        # honoring the actual Honolulu station config correctly denies it
        # (04:30 Honolulu is nowhere near 09:00-10:00).
        instant = dt.datetime(2026, 9, 30, 14, 30, 0, tzinfo=ZoneInfo("UTC"))
        result = self.check(instant)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, CODE_OUTSIDE_SCHEDULE_WINDOW)

        # The genuinely-matching Honolulu instant: 2026-09-30 09:30
        # Honolulu == 2026-09-30 19:30 UTC.
        matching_instant = dt.datetime(2026, 9, 30, 19, 30, 0, tzinfo=ZoneInfo("UTC"))
        self.assertTrue(self.check(matching_instant).allowed)


class StaffSuperuserScheduleBypassTests(ScheduledEvaluatorTestCase):
    def test_superuser_authorized_for_scheduled_capability_without_any_assignment(self):
        su = User.objects.create_superuser("sched_su", "su@example.invalid", "pw")
        result = authorize(su, "remote_dj.connect", now=station_dt(2026, 9, 30, 3, 0, 0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)

    def test_staff_authorized_for_scheduled_capability_without_any_assignment(self):
        staff = User.objects.create_user("sched_staff", "staff@example.invalid", "pw", is_staff=True)
        result = authorize(staff, "remote_dj.connect", now=station_dt(2026, 9, 30, 3, 0, 0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, CODE_ALLOWED)
