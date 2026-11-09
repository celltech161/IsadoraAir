"""[P0] 1.21: the DST fall-back transition identity and replay window (library.services.dst_fallback).

Pure time-zone arithmetic from controlled UTC instants -- independent of the host time zone and the real clock."""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from library.services import dst_fallback

UTC = dt_timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
FALL_BACK = date(2026, 11, 1)
SPRING_FORWARD = date(2026, 3, 8)


def utc(hour, minute=0, second=0, day=FALL_BACK):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


class ReferenceInstantTests(SimpleTestCase):
    def test_the_workorder_reference_instants(self):
        rows = [
            (utc(6, 59, 30), "01:59:30", "CDT", 0),
            (utc(7, 0, 0), "01:00:00", "CST", 1),
            (utc(7, 59, 30), "01:59:30", "CST", 1),
            (utc(8, 0, 0), "02:00:00", "CST", 0),
            (utc(9, 0, 0), "03:00:00", "CST", 0),
        ]
        for instant, wall, abbreviation, fold in rows:
            with self.subTest(instant=instant):
                local = instant.astimezone(CHICAGO)
                self.assertEqual((local.strftime("%H:%M:%S"), local.tzname(), local.fold), (wall, abbreviation, fold))
        # 02:00 CST is NOT an ambiguous fold=1 wall time: recognition cannot rely on now.fold.
        self.assertEqual(utc(8).astimezone(CHICAGO).fold, 0)


class TransitionIdentityTests(SimpleTestCase):
    def test_only_the_hour_after_the_repeated_hour_is_a_successor(self):
        for hour in range(24):
            with self.subTest(hour=hour):
                transition = dst_fallback.fallback_successor(FALL_BACK, hour, tz=CHICAGO)
                self.assertEqual(transition is not None, hour == 2)

    def test_the_transition_is_bound_to_actual_instants(self):
        t = dst_fallback.fallback_successor(FALL_BACK, 2, tz=CHICAGO, lead_seconds=30)
        self.assertEqual((t.first_instance_start, t.transition_at, t.hour_start), (utc(6), utc(7), utc(8)))
        self.assertEqual((t.offset_before, t.offset_after), (timedelta(hours=-5), timedelta(hours=-6)))
        self.assertEqual((t.repeated_date, t.repeated_hour, t.zone), (FALL_BACK, 1, "America/Chicago"))
        self.assertEqual((t.window_start, t.window_end), (utc(7, 59, 30), utc(9)))
        self.assertIn("America/Chicago fall-back 2026-11-01 01:00 UTC-05:00->UTC-06:00", t.identity)
        detail = t.as_detail()
        self.assertEqual((detail["transition_utc"], detail["log_hour_start_utc"]),
                         ("2026-11-01T07:00:00+00:00", "2026-11-01T08:00:00+00:00"))

    def test_the_window_is_bounded_in_instants_not_wall_labels(self):
        t = dst_fallback.fallback_successor(FALL_BACK, 2, tz=CHICAGO, lead_seconds=30)
        cases = [
            (utc(6, 59, 30), False),     # 01:59:30 CDT: the first instance of the repeated hour
            (utc(7, 0), False),          # 01:00 CST
            (utc(7, 59, 29), False),
            (utc(7, 59, 30), True),      # 01:59:30 CST: the next-hour lookahead
            (utc(8), True),              # 02:00 CST
            (utc(8, 59, 59), True),
            (utc(9), False),             # 03:00 CST: ordinary progression again
        ]
        for instant, open_ in cases:
            with self.subTest(instant=instant):
                self.assertEqual(t.window_open(instant), open_)
                # The same instant expressed in local time (fold set by astimezone) gives the same answer.
                self.assertEqual(t.window_open(instant.astimezone(CHICAGO)), open_)
        # A local wall time 01:59:30 with fold=0 (CDT) is an hour earlier than fold=1 (CST): never confused.
        self.assertFalse(t.window_open(datetime(2026, 11, 1, 1, 59, 30, tzinfo=CHICAGO, fold=0)))
        self.assertTrue(t.window_open(datetime(2026, 11, 1, 1, 59, 30, tzinfo=CHICAGO, fold=1)))

    def test_spring_forward_ordinary_days_and_other_zones_never_qualify(self):
        for day in (SPRING_FORWARD, date(2026, 11, 2), date(2026, 10, 31), date(2026, 7, 4)):
            for hour in range(24):
                with self.subTest(day=day, hour=hour):
                    self.assertIsNone(dst_fallback.fallback_successor(day, hour, tz=CHICAGO))
        for zone in ("UTC", "Asia/Tokyo", "America/Phoenix"):
            for hour in range(24):
                with self.subTest(zone=zone, hour=hour):
                    self.assertIsNone(dst_fallback.fallback_successor(FALL_BACK, hour, tz=ZoneInfo(zone)))
        # A half-hour shift (Lord Howe) repeats only part of an hour: not this design's case, never authorized.
        for hour in range(24):
            with self.subTest(zone="Australia/Lord_Howe", hour=hour):
                self.assertIsNone(dst_fallback.fallback_successor(date(2026, 4, 5), hour,
                                                                  tz=ZoneInfo("Australia/Lord_Howe")))

    def test_other_whole_hour_fall_backs_are_recognised_in_their_own_zone(self):
        london = dst_fallback.fallback_successor(date(2026, 10, 25), 2, tz=ZoneInfo("Europe/London"))
        self.assertEqual((london.transition_at, london.hour_start),
                         (datetime(2026, 10, 25, 1, tzinfo=UTC), datetime(2026, 10, 25, 2, tzinfo=UTC)))
        self.assertIsNone(dst_fallback.fallback_successor(date(2026, 10, 25), 1, tz=ZoneInfo("Europe/London")))
        new_york = dst_fallback.fallback_successor(FALL_BACK, 2, tz=ZoneInfo("America/New_York"))
        self.assertEqual(new_york.hour_start, utc(7))

    def test_invalid_hours_are_refused(self):
        for hour in (-1, 24, None, "2"):
            self.assertIsNone(dst_fallback.fallback_successor(FALL_BACK, hour, tz=CHICAGO))


class _Row:
    def __init__(self, id, position, origin, track_id=1, category_id=1, scheduled=0, played=None):
        self.id, self.position, self.origin = id, position, origin
        self.track_id, self.category_id = track_id, category_id
        self.scheduled_time = utc(8) + timedelta(minutes=scheduled)
        self.played_at = played
        self.category = None
        self.track = type("T", (), {"filepath": f"/m/{track_id}.flac"})()


class ReplayRecognitionTests(SimpleTestCase):
    def test_a_replay_row_matches_an_original_and_other_inserts_never_do(self):
        G, E = "generated_schedule", "engine_inserted"
        rows = [_Row(1, 0, G, track_id=1, scheduled=0), _Row(2, 1, G, track_id=2, scheduled=4),
                _Row(3, 2, E, track_id=9, scheduled=7),        # an urgent insert: its own Track and instant
                _Row(4, 3, E, track_id=1, scheduled=0)]        # the replay of row 1
        self.assertEqual([r.id for r in dst_fallback.replay_rows(rows)], [4])

    def test_only_ordinary_programming_that_aired_before_the_real_hour_is_a_source(self):
        t = dst_fallback.fallback_successor(FALL_BACK, 2, tz=CHICAGO)
        early, late = utc(7, 30), utc(8, 5)
        rows = [
            _Row(1, 0, "generated_schedule", played=early),
            _Row(2, 1, "generated_fill", played=early),
            _Row(3, 2, "live_fill", played=early),
            _Row(4, 3, "generated_schedule", played=late),           # aired in the real hour
            _Row(5, 4, "generated_schedule", played=None),           # never aired
            _Row(6, 5, "engine_inserted", played=early),             # urgent / native / dedication intro
            _Row(7, 6, "operator_inserted", played=early),
            _Row(8, 7, "refill_installed", played=early),            # Hour Refill install
            _Row(9, 8, None, played=early),                          # unknown writer (Play Now, admin)
            _Row(10, 10000, "generated_schedule", played=early),     # legacy insert position
            _Row(11, 9, "generated_schedule", played=early),         # a web request is attached
        ]
        sources = dst_fallback.replay_sources(rows, t, linked_ids=frozenset({11}))
        self.assertEqual([r.id for r in sources], [1, 2, 3])
