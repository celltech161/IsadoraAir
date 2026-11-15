"""Past Overrides: the selected profile's stored, explicit date-specific
schedule rows dated before today (station time), and their removal through the
existing scoped DELETE endpoint (docs/SCHEDULE_PROFILES.md, "Past Overrides")."""
from datetime import date, datetime, time, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from library import views
from library.models import LogItem, PlaylistLog, ScheduleBlock, ScheduleProfile
from library.tests.test_schedule_profiles_3_1b import ApiMixin

TODAY = date(2027, 3, 10)


def at(profile, on, hour, minute=0, *, rotation=None, playlist=None):
    return ScheduleBlock.objects.create(
        profile=profile, specific_date=on, start_time=time(hour, minute),
        end_time=time((hour + 1) % 24, 0), rotation=rotation, playlist=playlist,
    )


@override_settings(SECURE_SSL_REDIRECT=False)
@patch("library.views._station_local_today", lambda: TODAY)
class PastOverridesApiTests(ApiMixin, TestCase):
    def url(self, profile=None, **params):
        query = {"profile": str((profile or self.default).uuid), **params}
        return reverse("library:api-schedule-past-overrides") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def listing(self, profile=None, **params):
        response = self.client.get(self.url(profile, **params))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def flat(self, data):
        return [(group["date"], item["start_time"], item["content_name"])
                for group in data["dates"] for item in group["overrides"]]

    def test_only_dates_before_today_newest_first_with_every_explicit_minute(self):
        at(self.default, date(2027, 3, 9), 14, rotation=self.rotation_a)
        at(self.default, date(2027, 3, 9), 6, 37, rotation=self.rotation_b)     # minute transition
        at(self.default, date(2027, 3, 9), 6, rotation=self.rotation_a)
        at(self.default, date(2027, 2, 1), 0, playlist=self.playlist)
        at(self.default, TODAY, 8, rotation=self.rotation_b)                     # today: excluded
        at(self.default, date(2027, 3, 11), 8, rotation=self.rotation_b)         # future: excluded
        self.weekly(self.default, 9, dow=TODAY.weekday())                       # weekly: excluded
        data = self.listing()
        self.assertEqual(data["today"], "2027-03-10")
        self.assertEqual(self.flat(data), [
            ("2027-03-09", "06:00", "Rotation A"),
            ("2027-03-09", "06:37", "Rotation B"),
            ("2027-03-09", "14:00", "Rotation A"),
            ("2027-02-01", "00:00", "Playlist P"),
        ])
        minute_row = data["dates"][0]["overrides"][1]
        self.assertEqual((minute_row["minute"], minute_row["content_kind"], minute_row["specific_date"]),
                         (37, "rotation", "2027-03-09"))
        self.assertEqual(data["dates"][1]["overrides"][0]["content_kind"], "playlist")
        self.assertFalse(data["has_more"])
        self.assertIsNone(data["next_before"])

    def test_scoped_to_the_selected_profile(self):
        other = self.create_profile("Holiday")
        at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        at(other, date(2027, 3, 2), 7, rotation=self.rotation_b)
        self.assertEqual(self.flat(self.listing()), [("2027-03-01", "05:00", "Rotation A")])
        data = self.listing(other)
        self.assertEqual(data["profile_uuid"], str(other.uuid))
        self.assertEqual(self.flat(data), [("2027-03-02", "07:00", "Rotation B")])

    def test_empty_profile_and_name_filter(self):
        self.assertEqual(self.listing()["dates"], [])
        at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        at(self.default, date(2027, 3, 2), 7, playlist=self.playlist)
        self.assertEqual(self.flat(self.listing(q="playlist")), [("2027-03-02", "07:00", "Playlist P")])
        self.assertEqual(self.listing(q="nothing-like-this")["dates"], [])

    def test_paginated_by_whole_dates(self):
        for day in (1, 2, 3, 4, 5):
            at(self.default, date(2027, 3, day), 6, rotation=self.rotation_a)
            at(self.default, date(2027, 3, day), 6, 30, rotation=self.rotation_b)
        first = self.listing(limit=2)
        self.assertEqual([g["date"] for g in first["dates"]], ["2027-03-05", "2027-03-04"])
        self.assertTrue(all(len(g["overrides"]) == 2 for g in first["dates"]))
        self.assertEqual((first["has_more"], first["next_before"]), (True, "2027-03-04"))
        second = self.listing(limit=2, before=first["next_before"])
        self.assertEqual([g["date"] for g in second["dates"]], ["2027-03-03", "2027-03-02"])
        last = self.listing(limit=2, before=second["next_before"])
        self.assertEqual(([g["date"] for g in last["dates"]], last["has_more"]), (["2027-03-01"], False))
        # A cursor in the future never reveals today's or future rows.
        at(self.default, TODAY, 1, rotation=self.rotation_a)
        self.assertNotIn("2027-03-10", [g["date"] for g in self.listing(before="2030-01-01")["dates"]])

    def test_invalid_parameters_are_rejected(self):
        self.assertEqual(self.client.get(self.url(before="not-a-date")).status_code, 400)
        self.assertEqual(self.client.get(self.url(limit="many")).status_code, 400)
        self.assertEqual(self.client.get(
            reverse("library:api-schedule-past-overrides") + "?profile=not-a-uuid").status_code, 404)

    def test_archived_profile_is_listed_but_its_rows_cannot_be_removed(self):
        other = self.create_profile("Old season")
        row = at(other, date(2027, 1, 5), 9, rotation=self.rotation_a)
        other.is_archived = True
        other.save(update_fields=["is_archived"])
        data = self.listing(other)
        self.assertTrue(data["is_archived"])
        self.assertEqual(len(data["dates"]), 1)
        response = self.client.delete(
            reverse("library:api-schedule-delete", args=[row.pk]) + f"?profile={other.uuid}&date=2027-01-05")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(ScheduleBlock.objects.filter(pk=row.pk).exists())

    def test_reader_may_list_but_not_remove(self):
        row = at(self.default, date(2027, 3, 1), 5, rotation=self.rotation_a)
        self.client.force_login(self.reader)
        self.assertEqual(len(self.listing()["dates"]), 1)
        response = self.client.delete(
            reverse("library:api-schedule-delete", args=[row.pk]) + f"?profile={self.default.uuid}&date=2027-03-01")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ScheduleBlock.objects.filter(pk=row.pk).exists())

    def test_removing_one_override_leaves_siblings_weekly_other_dates_and_logs(self):
        target = at(self.default, date(2027, 3, 1), 6, 15, rotation=self.rotation_a)
        sibling = at(self.default, date(2027, 3, 1), 6, rotation=self.rotation_b)
        other_date = at(self.default, date(2027, 2, 1), 6, 15, rotation=self.rotation_a)
        weekly = self.weekly(self.default, 6, dow=date(2027, 3, 1).weekday())
        log = PlaylistLog.objects.create(date=date(2027, 3, 1), hour=6, status="approved",
                                         schedule_profile=self.default)
        before = (PlaylistLog.objects.count(), LogItem.objects.count())
        response = self.client.delete(
            reverse("library:api-schedule-delete", args=[target.pk]) + f"?profile={self.default.uuid}&date=2027-03-01")
        self.assertEqual(response.json(), {"ok": True, "deleted": True})
        self.assertFalse(ScheduleBlock.objects.filter(pk=target.pk).exists())
        for row in (sibling, other_date, weekly):
            self.assertTrue(ScheduleBlock.objects.filter(pk=row.pk).exists())
        self.assertTrue(PlaylistLog.objects.filter(pk=log.pk).exists())
        self.assertEqual((PlaylistLog.objects.count(), LogItem.objects.count()), before)
        self.assertEqual(self.flat(self.listing()), [
            ("2027-03-01", "06:00", "Rotation B"), ("2027-02-01", "06:15", "Rotation A"),
        ])

    def test_a_date_disappears_when_its_last_override_is_removed(self):
        only = at(self.default, date(2027, 3, 3), 4, rotation=self.rotation_a)
        at(self.default, date(2027, 3, 2), 4, rotation=self.rotation_a)
        self.client.delete(
            reverse("library:api-schedule-delete", args=[only.pk]) + f"?profile={self.default.uuid}&date=2027-03-03")
        self.assertEqual([g["date"] for g in self.listing()["dates"]], ["2027-03-02"])

    def test_a_stale_id_from_another_profile_removes_nothing(self):
        other = self.create_profile("Other")
        foreign = at(other, date(2027, 3, 1), 5, rotation=self.rotation_a)
        response = self.client.delete(
            reverse("library:api-schedule-delete", args=[foreign.pk]) + f"?profile={self.default.uuid}&date=2027-03-01")
        self.assertEqual(response.json(), {"ok": True, "deleted": False})
        self.assertTrue(ScheduleBlock.objects.filter(pk=foreign.pk).exists())

    def test_rotation_becomes_deletable_only_after_every_dated_reference_is_removed(self):
        other = self.create_profile("Spare")
        obsolete = self.rotation_b
        rows = [
            (self.default, at(self.default, date(2027, 1, 4), 10, rotation=obsolete)),
            (self.default, at(self.default, date(2026, 12, 25), 10, 30, rotation=obsolete)),
            (other, at(other, date(2027, 2, 14), 18, rotation=obsolete)),
        ]
        found = [item["id"] for profile in (self.default, other)
                 for group in self.listing(profile, q="Rotation B")["dates"] for item in group["overrides"]]
        self.assertEqual(sorted(found), sorted(row.pk for _profile, row in rows))
        for index, (profile, row) in enumerate(rows):
            with self.assertRaises(ProtectedError):
                obsolete.delete()
            response = self.client.delete(
                reverse("library:api-schedule-delete", args=[row.pk])
                + f"?profile={profile.uuid}&date={row.specific_date.isoformat()}")
            self.assertTrue(response.json()["deleted"], index)
        obsolete.delete()
        # The rotation that was not referenced by the removed rows is untouched.
        self.assertTrue(type(self.rotation_a).objects.filter(pk=self.rotation_a.pk).exists())


class StationTodayTests(TestCase):
    def test_today_is_the_station_date_not_the_utc_date(self):
        chicago = ZoneInfo("America/Chicago")
        # 03:30 UTC on 2 March is still 1 March (21:30 CST) at the station.
        instant = datetime(2027, 3, 2, 3, 30, tzinfo=dt_timezone.utc)
        with patch("library.middleware.get_station_timezone", lambda: chicago), \
                patch("django.utils.timezone.now", lambda: instant):
            self.assertEqual(views._station_local_today(), date(2027, 3, 1))


@override_settings(SECURE_SSL_REDIRECT=False)
class StationTodayBoundaryApiTests(ApiMixin, TestCase):
    def test_the_station_date_decides_what_is_past(self):
        chicago = ZoneInfo("America/Chicago")
        instant = datetime(2027, 3, 2, 3, 30, tzinfo=dt_timezone.utc)   # 1 March at the station
        at(self.default, date(2027, 3, 1), 20, rotation=self.rotation_a)   # station "today": excluded
        at(self.default, date(2027, 2, 28), 20, rotation=self.rotation_b)  # yesterday: listed
        # Only the view module's clock is moved (the session machinery keeps the real one).
        clock = SimpleNamespace(now=lambda: instant, localtime=django_timezone.localtime)
        with patch("library.middleware.get_station_timezone", lambda: chicago), \
                patch("library.views.timezone", clock):
            data = self.client.get(reverse("library:api-schedule-past-overrides")
                                   + f"?profile={self.default.uuid}").json()
        self.assertEqual(data["today"], "2027-03-01")
        self.assertEqual([g["date"] for g in data["dates"]], ["2027-02-28"])


class PastOverridesTemplateTests(TestCase):
    TEMPLATE = Path(__file__).resolve().parents[1] / "templates/library/schedule.html"

    def test_button_lives_in_the_date_override_controls_after_today(self):
        source = self.TEMPLATE.read_text()
        controls = source[source.index('<div id="dateControls"'):]
        controls = controls[: controls.index("</div>")]
        self.assertLess(controls.index('id="todayButton"'), controls.index('id="pastOverridesButton"'))
        self.assertIn(">Past Overrides</button>", controls)

    def test_list_requests_are_generation_bound_and_reset_on_mode_profile_and_date_changes(self):
        source = self.TEMPLATE.read_text()
        for required in (
            "pastOverridesRequestGeneration++", "function pastOverridesContextIsLive(ctx)",
            "ctx.generation === pastOverridesRequestGeneration", "selectedProfile.uuid === ctx.profileUuid",
            "/api/schedule/past-overrides/?profile=", "async function reloadScheduleView()",
            "scheduleMode === 'date' && !pastOverridesOpen", "window.confirm(message)",
        ):
            self.assertIn(required, source)
        set_mode = source[source.index("async function setMode(mode)"):]
        self.assertIn("showPastOverrides(false);", set_mode[: set_mode.index("\n}\n")])
        set_date = source[source.index("function setOverrideDate(isoDate)"):]
        self.assertIn("showPastOverrides(false);", set_date[: set_date.index("\n}\n")])
