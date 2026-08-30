"""weather/voice_schedule.py -- pure function, no DB needed. This is
the Django-owned replacement for weather-ingest/lib/voices.py's own
voice_for_hour(), extracted here specifically so Road Conditions (and
any future Django-side consumer) never needs to import the external
weather-ingest project for schedule resolution."""
from django.test import SimpleTestCase

from weather.voice_schedule import voice_for_hour


class VoiceForHourTests(SimpleTestCase):
    def test_day_slot(self):
        self.assertEqual(voice_for_hour(10, [["day", 6, 17], ["night", 18, 5]]), "day")

    def test_night_slot(self):
        self.assertEqual(voice_for_hour(20, [["day", 6, 17], ["night", 18, 5]]), "night")

    def test_wrapped_midnight_range(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        for hour in (18, 21, 23, 0, 5):
            with self.subTest(hour=hour):
                self.assertEqual(voice_for_hour(hour, schedule), "night")

    def test_boundary_hours_are_inclusive(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        self.assertEqual(voice_for_hour(6, schedule), "day")
        self.assertEqual(voice_for_hour(17, schedule), "day")
        self.assertEqual(voice_for_hour(18, schedule), "night")
        self.assertEqual(voice_for_hour(5, schedule), "night")

    def test_no_covering_entry_defaults_to_day(self):
        self.assertEqual(voice_for_hour(10, [["night", 18, 5]]), "day")

    def test_empty_schedule_defaults_to_day(self):
        self.assertEqual(voice_for_hour(10, []), "day")
