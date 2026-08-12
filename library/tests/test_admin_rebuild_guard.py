"""1.1 spec (2026-08-11) -- the narrow admin-rebuild guard (the relevant
slice of roadmap item 1.7): api_log_build must refuse to destructively
rebuild the hour that's currently active/on-air, whether or not the
engine is currently reporting fresh state."""
import json
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library.models import Category, CategoryKind, PlaylistLog, Rotation, RotationSlot, ScheduleBlock
from library.views import _is_active_or_imminent_hour


def make_schedule_block(target_date, hour, rotation):
    """specific_date match (not day_of_week) -- simplest deterministic
    ScheduleBlock fixture for resolve_schedule_block's exact
    start_time == time(hour, 0) matching."""
    return ScheduleBlock.objects.create(
        specific_date=target_date, start_time=time(hour, 0),
        end_time=time((hour + 1) % 24, 0), rotation=rotation,
    )


class IsActiveOrImminentHourTests(TestCase):
    def test_current_wall_clock_hour_is_active_even_without_engine_state(self):
        now = timezone.localtime()
        with patch("library.views._read_engine_state", return_value=None):
            self.assertTrue(_is_active_or_imminent_hour(now.date(), now.hour))

    def test_other_hour_is_not_active_without_engine_state(self):
        now = timezone.localtime()
        other_hour = (now.hour + 5) % 24
        other_date = now.date() if other_hour != now.hour else now.date() + timedelta(days=1)
        with patch("library.views._read_engine_state", return_value=None):
            self.assertFalse(_is_active_or_imminent_hour(other_date, other_hour))

    def test_engine_state_reports_early_rollover_to_next_hour(self):
        """Engine has already advanced its active queue to the NEXT
        hour ahead of real top-of-hour -- that hour must be protected
        too, even though wall-clock 'now' hasn't reached it yet."""
        now = timezone.localtime()
        next_hour_dt = now + timedelta(hours=1)
        fake_state = {"date": next_hour_dt.date().isoformat(), "hour": next_hour_dt.hour}
        with patch("library.views._read_engine_state", return_value=fake_state):
            self.assertTrue(_is_active_or_imminent_hour(next_hour_dt.date(), next_hour_dt.hour))

    def test_stale_or_missing_engine_state_does_not_protect_other_hours(self):
        now = timezone.localtime()
        other_hour = (now.hour + 5) % 24
        other_date = now.date() if other_hour != now.hour else now.date() + timedelta(days=1)
        with patch("library.views._read_engine_state", return_value=None):
            self.assertFalse(_is_active_or_imminent_hour(other_date, other_hour))

    def test_malformed_engine_state_date_does_not_crash(self):
        with patch("library.views._read_engine_state", return_value={"date": "not-a-date", "hour": 5}):
            self.assertFalse(_is_active_or_imminent_hour(date(2027, 1, 1), 5))


@override_settings(SECURE_SSL_REDIRECT=False)
class ApiLogBuildGuardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("guardtest", "guardtest@example.invalid", "pw")
        self.client.force_login(self.staff)
        kind = CategoryKind.objects.create(code="guardtest-kind", name="Guard Test")
        self.category = Category.objects.create(code="GUARDTEST", name="Guard Test", kind=kind)
        self.rotation = Rotation.objects.create(name="Guard Test Rotation")
        RotationSlot.objects.create(rotation=self.rotation, position=0, category=self.category)

    def post_build(self, target_date, hour):
        return self.client.post(
            reverse("library:api-log-build"),
            data=json.dumps({"date": target_date.isoformat(), "hour": hour}),
            content_type="application/json",
        )

    def test_rebuild_blocked_for_current_hour(self):
        now = timezone.localtime()
        make_schedule_block(now.date(), now.hour, self.rotation)
        with patch("library.views._read_engine_state", return_value=None):
            resp = self.post_build(now.date(), now.hour)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("active", resp.json()["error"].lower())
        # Never touched the DB -- no PlaylistLog created for this hour.
        self.assertFalse(PlaylistLog.objects.filter(date=now.date(), hour=now.hour).exists())

    def test_rebuild_allowed_for_a_different_hour(self):
        now = timezone.localtime()
        other_hour = (now.hour + 5) % 24
        other_date = now.date() if other_hour != now.hour else now.date() + timedelta(days=1)
        make_schedule_block(other_date, other_hour, self.rotation)
        with patch("library.views._read_engine_state", return_value=None):
            resp = self.post_build(other_date, other_hour)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(PlaylistLog.objects.filter(date=other_date, hour=other_hour).exists())

    def test_rebuild_blocked_when_engine_reports_active_on_a_different_hour(self):
        """The engine's early-rollover-advanced hour is protected even
        though it isn't the wall-clock current hour."""
        now = timezone.localtime()
        engine_active_hour = (now.hour + 3) % 24
        engine_active_date = now.date() if engine_active_hour != now.hour else now.date() + timedelta(days=1)
        make_schedule_block(engine_active_date, engine_active_hour, self.rotation)
        fake_state = {"date": engine_active_date.isoformat(), "hour": engine_active_hour}
        with patch("library.views._read_engine_state", return_value=fake_state):
            resp = self.post_build(engine_active_date, engine_active_hour)
        self.assertEqual(resp.status_code, 409)
