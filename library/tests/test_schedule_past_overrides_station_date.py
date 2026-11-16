"""Past Overrides through the real station-time path (r0141 production defect).

r0141 shipped ``_station_local_today()`` passing ``get_station_timezone()`` -- the configured zone NAME, a
``str`` read from ``StationTimeConfig`` -- straight to ``django.utils.timezone.localtime()``, which needs a
``tzinfo``. Every real request raised ``TypeError`` and returned HTTP 500. The original tests hid it: they
patched ``_station_local_today()`` itself, or replaced ``get_station_timezone()`` with a ``ZoneInfo``.

Nothing here patches ``_station_local_today()`` or ``get_station_timezone()``. The station zone is stored
as a string in ``StationTimeConfig``; the authenticated client requests ``/api/schedule/past-overrides/``
through the full middleware stack against PostgreSQL. Where a test needs a fixed instant, only the view
module's ``timezone.now`` is moved (the session machinery keeps the real clock).
"""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from library import middleware, views
from library.models import ScheduleBlock, StationTimeConfig
from library.tests.test_schedule_profiles_3_1b import ApiMixin

UTC = dt_timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class _Clock:
    """Stands in for ``library.views.timezone``: a fixed ``now()``, everything else real."""

    def __init__(self, instant):
        self._instant = instant

    def now(self):
        return self._instant

    def __getattr__(self, name):
        return getattr(django_timezone, name)


def frozen(instant):
    return patch.object(views, "timezone", _Clock(instant))


def set_station_zone(name):
    """Store the zone exactly as the admin form does: a string on the singleton (post_save invalidates the
    middleware cache)."""
    config = StationTimeConfig.load()
    config.timezone = name
    config.save()


class StationZoneMixin(ApiMixin):
    ZONE = "America/Chicago"

    def setUp(self):
        middleware._invalidate_station_tz_cache()
        # The cache outlives the rolled-back test transaction; never leak this test's zone.
        self.addCleanup(middleware._invalidate_station_tz_cache)
        super().setUp()
        set_station_zone(self.ZONE)

    def url(self, profile=None, **params):
        query = {"profile": str((profile or self.default).uuid), **params}
        return reverse("library:api-schedule-past-overrides") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def listing(self, profile=None, **params):
        response = self.client.get(self.url(profile, **params))
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response["Content-Type"], "application/json")
        return response.json()

    def at(self, profile, on, hour, minute=0, *, rotation=None, playlist=None):
        return ScheduleBlock.objects.create(
            profile=profile, specific_date=on, start_time=time(hour, minute),
            end_time=time((hour + 1) % 24, 0), rotation=rotation, playlist=playlist,
        )

    @staticmethod
    def flat(data):
        return [(group["date"], item["start_time"], item["content_name"])
                for group in data["dates"] for item in group["overrides"]]


@override_settings(SECURE_SSL_REDIRECT=False)
class UnmockedProductionPathTests(StationZoneMixin, TestCase):
    """The exact production call path: string zone, real getter, real helper, real clock."""

    def test_the_configured_zone_really_is_a_string(self):
        self.assertEqual(middleware.get_station_timezone(), "America/Chicago")
        self.assertIsInstance(middleware.get_station_timezone(), str)

    def test_the_endpoint_answers_200_with_the_station_date(self):
        before = datetime.now(ZoneInfo(self.ZONE)).date()
        self.at(self.default, before - timedelta(days=1), 6, rotation=self.rotation_a)
        self.at(self.default, before - timedelta(days=1), 6, 37, rotation=self.rotation_b)
        self.at(self.default, before + timedelta(days=1), 6, rotation=self.rotation_a)
        today_row = self.at(self.default, before, 6, playlist=self.playlist)
        response = self.client.get(reverse("library:api-schedule-past-overrides") + f"?profile={self.default.uuid}")
        after = datetime.now(ZoneInfo(self.ZONE)).date()
        self.assertEqual(response.status_code, 200, response.content[:500])
        data = response.json()
        self.assertIn(data["today"], {before.isoformat(), after.isoformat()})
        yesterday = (before - timedelta(days=1)).isoformat()
        self.assertEqual(data["dates"][-1]["date"], yesterday)
        self.assertEqual([(i["start_time"], i["content_name"]) for i in data["dates"][-1]["overrides"]],
                         [("06:00", "Rotation A"), ("06:37", "Rotation B")])
        listed = {group["date"] for group in data["dates"]}
        self.assertNotIn((before + timedelta(days=1)).isoformat(), listed)
        if before == after:   # a request that straddled station midnight legitimately lists `before`
            self.assertNotIn(today_row.specific_date.isoformat(), listed)

    def test_the_helper_returns_the_station_date(self):
        self.assertIn(views._station_local_today(),
                      {datetime.now(ZoneInfo(self.ZONE)).date() + timedelta(days=d) for d in (0, 1)})


@override_settings(SECURE_SSL_REDIRECT=False)
class StationDateListingTests(StationZoneMixin, TestCase):
    TODAY_INSTANT = utc(2027, 3, 10, 18, 0)   # noon CST, 10 March

    def listing_at(self, instant=None, profile=None, **params):
        with frozen(instant or self.TODAY_INSTANT):
            return self.listing(profile, **params)

    def test_no_historical_overrides_is_an_empty_success(self):
        self.weekly(self.default, 6)   # weekly rows never appear
        self.at(self.default, date(2027, 3, 10), 6, rotation=self.rotation_a)   # today
        data = self.listing_at()
        self.assertEqual((data["dates"], data["has_more"], data["next_before"]), ([], False, None))
        self.assertEqual((data["today"], data["profile_uuid"], data["query"]),
                         ("2027-03-10", str(self.default.uuid), ""))

    def test_past_overrides_newest_first_with_every_explicit_minute(self):
        self.at(self.default, date(2027, 3, 9), 6, rotation=self.rotation_a)
        self.at(self.default, date(2027, 3, 9), 6, 37, playlist=self.playlist)
        self.at(self.default, date(2026, 12, 25), 23, 59, rotation=self.rotation_b)
        data = self.listing_at()
        self.assertEqual(self.flat(data), [
            ("2027-03-09", "06:00", "Rotation A"), ("2027-03-09", "06:37", "Playlist P"),
            ("2026-12-25", "23:59", "Rotation B"),
        ])
        minute_row = data["dates"][0]["overrides"][1]
        self.assertEqual((minute_row["minute"], minute_row["specific_date"], minute_row["content_kind"]),
                         (37, "2027-03-09", "playlist"))

    def test_today_and_future_overrides_are_excluded(self):
        self.at(self.default, date(2027, 3, 9), 23, rotation=self.rotation_a)
        self.at(self.default, date(2027, 3, 10), 0, rotation=self.rotation_a)
        self.at(self.default, date(2027, 3, 11), 0, rotation=self.rotation_a)
        self.at(self.default, date(2028, 1, 1), 0, rotation=self.rotation_a)
        self.assertEqual([g["date"] for g in self.listing_at()["dates"]], ["2027-03-09"])
        # A cursor beyond today never reveals today's or future rows.
        self.assertEqual([g["date"] for g in self.listing_at(before="2030-01-01")["dates"]], ["2027-03-09"])

    def test_near_midnight_utc_the_station_date_decides(self):
        self.at(self.default, date(2027, 3, 1), 20, rotation=self.rotation_a)
        self.at(self.default, date(2027, 2, 28), 20, rotation=self.rotation_b)
        # 03:30 UTC on 2 March is 21:30 CST on 1 March: 1 March is still today at the station.
        data = self.listing_at(utc(2027, 3, 2, 3, 30))
        self.assertEqual((data["today"], [g["date"] for g in data["dates"]]), ("2027-03-01", ["2027-02-28"]))
        # 06:30 UTC is 00:30 CST on 2 March: 1 March has become the past.
        data = self.listing_at(utc(2027, 3, 2, 6, 30))
        self.assertEqual((data["today"], [g["date"] for g in data["dates"]]), ("2027-03-02", ["2027-03-01", "2027-02-28"]))

    def test_a_station_ahead_of_utc_uses_its_own_date(self):
        set_station_zone("Pacific/Auckland")
        self.at(self.default, date(2027, 3, 1), 20, rotation=self.rotation_a)
        # 12:30 UTC on 1 March is 01:30 NZDT on 2 March: 1 March is already past at the station.
        data = self.listing_at(utc(2027, 3, 1, 12, 30))
        self.assertEqual((data["today"], [g["date"] for g in data["dates"]]), ("2027-03-02", ["2027-03-01"]))

    def test_station_date_across_dst_transitions(self):
        cases = [
            # Spring forward, Sunday 14 March 2027 02:00 CST -> 03:00 CDT (08:00 UTC).
            (utc(2027, 3, 14, 5, 59), "2027-03-13"),   # 23:59 CST
            (utc(2027, 3, 14, 6, 0), "2027-03-14"),    # 00:00 CST
            (utc(2027, 3, 14, 8, 30), "2027-03-14"),   # 03:30 CDT, just after the gap
            (utc(2027, 3, 15, 4, 59), "2027-03-14"),   # 23:59 CDT (UTC-5 now)
            (utc(2027, 3, 15, 5, 0), "2027-03-15"),    # 00:00 CDT
            # Fall back, Sunday 7 November 2027 02:00 CDT -> 01:00 CST (07:00 UTC).
            (utc(2027, 11, 7, 4, 59), "2027-11-06"),   # 23:59 CDT
            (utc(2027, 11, 7, 5, 0), "2027-11-07"),    # 00:00 CDT
            (utc(2027, 11, 7, 6, 30), "2027-11-07"),   # 01:30 CDT, first pass
            (utc(2027, 11, 7, 7, 30), "2027-11-07"),   # 01:30 CST, repeated hour
            (utc(2027, 11, 8, 5, 59), "2027-11-07"),   # 23:59 CST (UTC-6 again)
            (utc(2027, 11, 8, 6, 0), "2027-11-08"),    # 00:00 CST
        ]
        for day in (date(2027, 3, 13), date(2027, 3, 14), date(2027, 11, 6), date(2027, 11, 7)):
            self.at(self.default, day, 1, rotation=self.rotation_a)
        for instant, station_today in cases:
            with self.subTest(instant=instant.isoformat()):
                data = self.listing_at(instant)
                self.assertEqual(data["today"], station_today)
                self.assertTrue(all(g["date"] < station_today for g in data["dates"]), data["dates"])
                expected = sorted((d.isoformat() for d in (date(2027, 3, 13), date(2027, 3, 14),
                                                          date(2027, 11, 6), date(2027, 11, 7))
                                   if d.isoformat() < station_today), reverse=True)
                self.assertEqual([g["date"] for g in data["dates"]], expected)

    def test_rotation_and_playlist_name_filtering(self):
        self.at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        self.at(self.default, date(2027, 3, 2), 7, playlist=self.playlist)
        self.at(self.default, date(2027, 3, 3), 8, rotation=self.rotation_b)
        self.assertEqual(self.flat(self.listing_at(q="playlist")), [("2027-03-02", "07:00", "Playlist P")])
        self.assertEqual(self.flat(self.listing_at(q="ROTATION%20B")), [("2027-03-03", "08:00", "Rotation B")])
        self.assertEqual([g["date"] for g in self.listing_at(q="rotation")["dates"]], ["2027-03-03", "2027-03-01"])
        data = self.listing_at(q="%20%20Playlist%20%20")
        self.assertEqual((data["query"], len(data["dates"])), ("Playlist", 1))
        self.assertEqual(self.listing_at(q="nothing-like-this")["dates"], [])
        self.assertEqual(len(self.listing_at(q="x" * 150)["query"]), 100)

    def test_multiple_schedule_profiles_are_kept_apart(self):
        other = self.create_profile("Holiday")
        self.at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        self.at(other, date(2027, 3, 2), 7, rotation=self.rotation_b)
        self.at(other, date(2027, 3, 11), 7, rotation=self.rotation_b)   # future in the other profile
        self.assertEqual(self.flat(self.listing_at()), [("2027-03-01", "05:00", "Rotation A")])
        data = self.listing_at(profile=other)
        self.assertEqual((data["profile_uuid"], self.flat(data)),
                         (str(other.uuid), [("2027-03-02", "07:00", "Rotation B")]))

    def test_pagination_across_several_historical_dates(self):
        for day in range(1, 8):
            self.at(self.default, date(2027, 3, day), 6, rotation=self.rotation_a)
            self.at(self.default, date(2027, 3, day), 6, 30, rotation=self.rotation_b)
        pages, cursor = [], None
        while True:
            params = {"limit": 3, **({"before": cursor} if cursor else {})}
            data = self.listing_at(**params)
            self.assertTrue(all(len(g["overrides"]) == 2 for g in data["dates"]))   # a date is never split
            pages.append([g["date"][-2:] for g in data["dates"]])
            if not data["has_more"]:
                self.assertIsNone(data["next_before"])
                break
            self.assertEqual(data["next_before"], data["dates"][-1]["date"])
            cursor = data["next_before"]
        self.assertEqual(pages, [["07", "06", "05"], ["04", "03", "02"], ["01"]])

    def test_invalid_query_parameters(self):
        self.at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        with frozen(self.TODAY_INSTANT):
            for params in ({"before": "not-a-date"}, {"before": "2027-02-30"}, {"limit": "many"}, {"limit": "1.5"}):
                with self.subTest(params=params):
                    response = self.client.get(self.url(**params))
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"error": "before must be YYYY-MM-DD and limit an integer"})
            self.assertEqual(self.client.get(
                reverse("library:api-schedule-past-overrides") + "?profile=not-a-uuid").status_code, 404)
            self.assertEqual(self.client.post(self.url()).status_code, 405)

    def test_limits_are_clamped(self):
        ScheduleBlock.objects.bulk_create([
            ScheduleBlock(profile=self.default, specific_date=date(2027, 3, 9) - timedelta(days=n),
                          start_time=time(6, 0), end_time=time(7, 0), rotation=self.rotation_a)
            for n in range(105)
        ])
        for limit, count in (("0", 1), ("-4", 1), ("1000", 100), ("", 30)):
            with self.subTest(limit=limit):
                data = self.listing_at(limit=limit)
                self.assertEqual((len(data["dates"]), data["has_more"]), (count, True))


@override_settings(SECURE_SSL_REDIRECT=False, TIME_ZONE="Pacific/Auckland")
class StationZoneFallbackTests(StationZoneMixin, TestCase):
    """The middleware's established policy: an unusable station zone never takes the site down; the request
    runs in settings.TIME_ZONE (``timezone.deactivate()``). Past Overrides follows the same policy."""
    INSTANT = utc(2027, 3, 1, 12, 30)   # 06:30 CST 1 March; 01:30 NZDT 2 March

    def setUp(self):
        super().setUp()
        self.at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)

    def test_an_unknown_zone_name_falls_back_to_the_default_timezone(self):
        StationTimeConfig.objects.filter(pk=1).update(timezone="Not/A_Zone")   # bypasses form validation
        middleware._invalidate_station_tz_cache()
        self.assertEqual(middleware.get_station_timezone(), "Not/A_Zone")
        with frozen(self.INSTANT):
            data = self.listing()
        self.assertEqual((data["today"], [g["date"] for g in data["dates"]]), ("2027-03-02", ["2027-03-01"]))

    def test_an_unreadable_station_config_falls_back_to_the_default_timezone(self):
        middleware._invalidate_station_tz_cache()
        with patch.object(StationTimeConfig, "load", side_effect=RuntimeError("database unavailable")):
            self.assertEqual(middleware.get_station_timezone(), "Pacific/Auckland")
            with frozen(self.INSTANT):
                data = self.listing()
        self.assertEqual((data["today"], [g["date"] for g in data["dates"]]), ("2027-03-02", ["2027-03-01"]))

    def test_a_valid_zone_still_wins_over_the_default(self):
        with frozen(self.INSTANT):
            data = self.listing()
        self.assertEqual((data["today"], data["dates"]), ("2027-03-01", []))
