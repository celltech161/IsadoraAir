"""weather/voice_schedule.py -- pure function, no DB needed. This is
the Django-owned replacement for weather-ingest/lib/voices.py's own
voice_for_hour(), extracted here specifically so Road Conditions (and
any future Django-side consumer) never needs to import the external
weather-ingest project for schedule resolution."""
from django.test import SimpleTestCase

from weather.voice_schedule import ScheduleError, compress_from_hours, expand_to_hours, voice_for_hour


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


PRODUCTION_SCHEDULE = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]


class ExpandToHoursTests(SimpleTestCase):
    """r0028: [voice, start, end] triples -> {0..23: voice}, item 1 of
    the required round-trip coverage."""

    def test_valid_production_schedule_expands_to_24_hours(self):
        hours = expand_to_hours(PRODUCTION_SCHEDULE)
        self.assertEqual(set(hours), set(range(24)))
        self.assertEqual(hours[0], "night")   # wrapped range
        self.assertEqual(hours[3], "day")
        self.assertEqual(hours[8], "day")
        self.assertEqual(hours[9], "night")
        self.assertEqual(hours[20], "day")
        self.assertEqual(hours[21], "night")
        self.assertEqual(hours[2], "night")

    def test_single_entry_covering_whole_day(self):
        self.assertEqual(expand_to_hours([["day", 0, 23]]), {h: "day" for h in range(24)})

    def test_rejects_non_list(self):
        with self.assertRaises(ScheduleError):
            expand_to_hours("not a list")

    def test_rejects_malformed_entry_shape(self):
        with self.assertRaises(ScheduleError):
            expand_to_hours([["day", 0]])  # only 2 elements

    def test_rejects_out_of_range_hour(self):
        with self.assertRaises(ScheduleError):
            expand_to_hours([["day", 0, 24]])

    def test_rejects_bool_as_hour(self):
        # bool is an int subclass in Python -- must not silently pass.
        with self.assertRaises(ScheduleError):
            expand_to_hours([["day", False, 23]])

    def test_gap_rejected(self):
        with self.assertRaises(ScheduleError) as ctx:
            expand_to_hours([["day", 0, 10]])
        self.assertIn("no entry covering", str(ctx.exception))

    def test_overlap_rejected(self):
        with self.assertRaises(ScheduleError) as ctx:
            expand_to_hours([["day", 0, 12], ["night", 10, 23]])
        self.assertIn("overlapping", str(ctx.exception))


class CompressFromHoursTests(SimpleTestCase):
    """r0028: {0..23: voice} -> minimal [voice, start, end] triples,
    item 2 of the required round-trip coverage."""

    def test_expands_and_compresses_production_schedule_byte_identical(self):
        hours = expand_to_hours(PRODUCTION_SCHEDULE)
        self.assertEqual(compress_from_hours(hours), PRODUCTION_SCHEDULE)

    def test_whole_day_one_persona_compresses_to_single_entry(self):
        self.assertEqual(compress_from_hours({h: "day" for h in range(24)}), [["day", 0, 23]])

    def test_midnight_crossing_range_reconstructed(self):
        hours = {h: ("night" if h >= 21 or h <= 2 else "day") for h in range(24)}
        result = compress_from_hours(hours)
        self.assertIn(["night", 21, 2], result)
        self.assertIn(["day", 3, 20], result)

    def test_rejects_incomplete_hour_set(self):
        with self.assertRaises(ScheduleError):
            compress_from_hours({h: "day" for h in range(23)})  # missing hour 23

    def test_rejects_extra_unknown_hour_key(self):
        hours = {h: "day" for h in range(24)}
        hours[24] = "day"
        with self.assertRaises(ScheduleError):
            compress_from_hours(hours)

    def test_rejects_empty_persona_value(self):
        hours = {h: "day" for h in range(24)}
        hours[5] = ""
        with self.assertRaises(ScheduleError):
            compress_from_hours(hours)


class RoundTripStabilityTests(SimpleTestCase):
    """Item 3: round-trip stability -- expand then compress must
    reproduce the exact original canonical schedule, for every legal
    shape including a wrap at hour 0 itself."""

    def test_production_schedule_round_trips_exactly(self):
        self.assertEqual(compress_from_hours(expand_to_hours(PRODUCTION_SCHEDULE)), PRODUCTION_SCHEDULE)

    def test_two_entry_schedule_round_trips(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        self.assertEqual(compress_from_hours(expand_to_hours(schedule)), schedule)

    def test_wrap_starting_exactly_at_hour_zero(self):
        # The wrapped run itself starts at hour 0 -- no separate merge
        # step should be needed since compress's own boundary-detection
        # already treats the 24-hour set as a circle.
        schedule = [["night", 0, 5], ["day", 6, 23]]
        self.assertEqual(compress_from_hours(expand_to_hours(schedule)), schedule)

    def test_three_persona_schedule_round_trips(self):
        schedule = [["dawn", 5, 7], ["day", 8, 17], ["night", 18, 4]]
        self.assertEqual(compress_from_hours(expand_to_hours(schedule)), schedule)
