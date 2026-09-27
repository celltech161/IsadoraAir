"""Roadmap 2.5B -- TalentAssignment/ScheduleAccessConfig model-level
contracts not already covered by test_scheduled_evaluator.py's
evaluator-focused suite: the exactly-one-of constraint and the
singleton config pattern."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from authz.models import ScheduleAccessConfig, TalentAssignment

User = get_user_model()


class TalentAssignmentConstraintTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("constraint_test_user", password="pw")

    def test_neither_day_nor_date_set_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TalentAssignment.objects.create(
                user=self.user, start_time=dt.time(18, 0), end_time=dt.time(20, 0),
            )

    def test_both_day_and_date_set_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TalentAssignment.objects.create(
                user=self.user, day_of_week=2, specific_date=dt.date(2026, 9, 30),
                start_time=dt.time(18, 0), end_time=dt.time(20, 0),
            )

    def test_exactly_day_of_week_is_accepted(self):
        a = TalentAssignment.objects.create(
            user=self.user, day_of_week=2, start_time=dt.time(18, 0), end_time=dt.time(20, 0),
        )
        self.assertIsNotNone(a.pk)

    def test_exactly_specific_date_is_accepted(self):
        a = TalentAssignment.objects.create(
            user=self.user, specific_date=dt.date(2026, 9, 30),
            start_time=dt.time(18, 0), end_time=dt.time(20, 0),
        )
        self.assertIsNotNone(a.pk)

    def test_crosses_midnight_property(self):
        overnight = TalentAssignment(start_time=dt.time(22, 0), end_time=dt.time(2, 0))
        same_day = TalentAssignment(start_time=dt.time(18, 0), end_time=dt.time(20, 0))
        exact_equal = TalentAssignment(start_time=dt.time(18, 0), end_time=dt.time(18, 0))
        self.assertTrue(overnight.crosses_midnight)
        self.assertFalse(same_day.crosses_midnight)
        self.assertTrue(exact_equal.crosses_midnight)  # end<=start, per the documented rule

    def test_str_includes_user_and_window(self):
        a = TalentAssignment.objects.create(
            user=self.user, day_of_week=5, start_time=dt.time(22, 0), end_time=dt.time(2, 0),
        )
        text = str(a)
        self.assertIn(self.user.username, text)
        self.assertIn("22:00", text)
        self.assertIn("overnight", text)


class ScheduleAccessConfigSingletonTests(TestCase):
    def test_seed_migration_already_populated_the_singleton_with_defaults(self):
        # authz.migrations.0004_seed_schedule_access_config seeds this row
        # on every migrated database, including the test database -- so
        # by the time any test runs, pk=1 already exists with the
        # documented defaults.
        self.assertEqual(ScheduleAccessConfig.objects.count(), 1)
        cfg = ScheduleAccessConfig.load()
        self.assertEqual(cfg.pk, 1)
        self.assertEqual(cfg.pre_schedule_allowance_minutes, 10)
        self.assertEqual(cfg.post_schedule_allowance_minutes, 15)

    def test_load_creates_defaults_if_the_row_is_somehow_missing(self):
        ScheduleAccessConfig.objects.all().delete()
        cfg = ScheduleAccessConfig.load()
        self.assertEqual(cfg.pk, 1)
        self.assertEqual(cfg.pre_schedule_allowance_minutes, 10)
        self.assertEqual(cfg.post_schedule_allowance_minutes, 15)

    def test_load_returns_the_same_row_on_repeated_calls(self):
        first = ScheduleAccessConfig.load()
        first.pre_schedule_allowance_minutes = 42
        first.save()
        second = ScheduleAccessConfig.load()
        self.assertEqual(second.pk, 1)
        self.assertEqual(second.pre_schedule_allowance_minutes, 42)
        self.assertEqual(ScheduleAccessConfig.objects.count(), 1)

    def test_zero_allowances_are_valid(self):
        cfg = ScheduleAccessConfig.load()
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.full_clean()
        cfg.save()
        cfg.refresh_from_db()
        self.assertEqual(cfg.pre_schedule_allowance_minutes, 0)
        self.assertEqual(cfg.post_schedule_allowance_minutes, 0)

    def test_negative_allowance_is_rejected_by_validation(self):
        cfg = ScheduleAccessConfig.load()
        cfg.pre_schedule_allowance_minutes = -1
        with self.assertRaises(Exception):
            cfg.full_clean()
