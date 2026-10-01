"""Roadmap 3.1C -- minute-resolution scheduling.

Resolution semantics, write safety, the schedule API, the server-derived Hour
Detail response, and the combined-hour builder (one PlaylistLog per wall-clock
hour built from several segments, persisted once).
"""
import json
import tempfile
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from library.models import (
    Artist, Category, CategoryKind, LogFillConfig, LogItem, Playlist, PlaylistItem,
    PlaylistLog, Rotation, RotationSlot, ScheduleBlock, ScheduleProfile, ScheduleProfileState, Track,
)
from library.services import log_builder
from library.services.log_builder import (
    LOCK_CONTENDED,
    build_and_approve_hour_log_locked,
    build_hour_log,
    build_hour_log_for_admin,
    effective_airtime_seconds,
    preview_hour_log,
    resolve_schedule_block,
)
from library.services.schedule_resolution import (
    effective_segments, load_hour_rows, minute_map, resolve_schedule_segments,
)
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_schedule_profiles_3_1a import ScheduleFixtureMixin
from library.tests.test_schedule_profiles_3_1b import ApiMixin

MONDAY = date(2027, 3, 1)        # weekday() == 0
NEXT_MONDAY = date(2027, 3, 8)
TUESDAY = date(2027, 3, 2)


def make_row(profile, hour, minute, *, rotation=None, playlist=None, dow=None, on=None):
    return ScheduleBlock.objects.create(
        profile=profile, day_of_week=dow, specific_date=on, start_time=time(hour, minute),
        end_time=time((hour + 1) % 24, 0), rotation=rotation, playlist=playlist,
    )


def shape(segments):
    """[(start_minute, content name, origin)] for compact assertions."""
    return [(s.start_minute, s.content.name, s.origin) for s in segments]


class ResolverFixtures(ScheduleFixtureMixin):
    def build_resolver_fixtures(self, base_fixtures=True):
        if base_fixtures:
            self.build_fixtures()
        self.profile = ensure_schedule_profile_state().active_profile
        self.A = Rotation.objects.create(name="A")
        self.B = Rotation.objects.create(name="B")
        self.C = Rotation.objects.create(name="C")
        self.D = Rotation.objects.create(name="D")
        self.E = Rotation.objects.create(name="E")
        self.other = ScheduleProfile.objects.create(name="Other Profile")


class SegmentResolutionTests(ResolverFixtures, TestCase):
    def setUp(self):
        self.build_resolver_fixtures()

    def segments(self, profile=None, target_date=MONDAY, hour=10):
        return resolve_schedule_segments(target_date, hour, profile or self.profile)

    def test_base_only_is_one_segment(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly")])

    def test_base_plus_transition_is_two_segments(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly"), (30, "B", "weekly")])

    def test_returning_to_the_base_content_is_a_third_segment_without_materializing_minutes(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 45, rotation=self.A, dow=0)
        self.assertEqual(
            shape(self.segments()), [(0, "A", "weekly"), (30, "B", "weekly"), (45, "A", "weekly")],
        )
        # Exactly the three transition rows exist -- never one per minute.
        self.assertEqual(ScheduleBlock.objects.filter(profile=self.profile).count(), 3)

    def test_same_exact_minute_is_allowed_in_another_profile_and_profiles_are_isolated(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.other, 10, 0, rotation=self.C, dow=0)
        make_row(self.other, 10, 30, rotation=self.D, dow=0)
        self.assertEqual(shape(self.segments(self.profile)), [(0, "A", "weekly"), (30, "B", "weekly")])
        self.assertEqual(shape(self.segments(self.other)), [(0, "C", "weekly"), (30, "D", "weekly")])

    def test_missing_base_never_falls_through_to_another_profile(self):
        make_row(self.other, 10, 0, rotation=self.C, dow=0)
        self.assertEqual(self.segments(self.profile), [])

    def test_dated_transition_layers_over_the_weekly_base(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.D, on=MONDAY)
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly"), (30, "D", "date_override")])
        # A different date still sees only the weekly layer.
        self.assertEqual(shape(self.segments(target_date=NEXT_MONDAY)), [(0, "A", "weekly")])

    def test_a_later_weekly_transition_does_not_punch_through_an_active_dated_layer(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 45, rotation=self.C, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly"), (20, "D", "date_override")])
        weekly, dated = load_hour_rows(self.profile, MONDAY, 10)
        entries = minute_map(weekly, dated, layer="date")
        self.assertEqual(entries[19]["effective_block"].rotation.name, "A")
        for minute in (20, 30, 45, 59):
            self.assertEqual(entries[minute]["effective_block"].rotation.name, "D", minute)
            self.assertEqual(entries[minute]["origin"], "date_override")

    def inherited(self, entries):
        return [e["minute"] for e in entries if e["inherited_transition"]]

    def date_entries(self):
        weekly, dated = load_hour_rows(self.profile, MONDAY, 10)
        return minute_map(weekly, dated, layer="date")

    def test_a_weekly_transition_shadowed_by_an_active_dated_row_is_not_inherited(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        entries = self.date_entries()
        self.assertEqual(entries[30]["effective_block"].rotation.name, "D")
        self.assertEqual(entries[30]["origin"], "date_override")
        self.assertFalse(entries[30]["inherited_transition"])
        self.assertFalse(entries[30]["segment_start"])

    def test_a_dated_base_shadows_every_later_weekly_transition(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 0, rotation=self.D, on=MONDAY)
        entries = self.date_entries()
        self.assertEqual(self.inherited(entries), [])
        self.assertEqual(entries[0]["explicit_block"].rotation.name, "D")

    def test_weekly_transitions_before_the_first_dated_row_are_still_inherited(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 10, rotation=self.C, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        entries = self.date_entries()
        self.assertEqual(self.inherited(entries), [0, 10])  # 10:30 is shadowed
        self.assertEqual(entries[10]["origin"], "weekly")

    def test_a_genuinely_inherited_weekly_transition_is_identified(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 45, rotation=self.D, on=MONDAY)
        entries = self.date_entries()
        self.assertEqual(self.inherited(entries), [0, 30])
        self.assertEqual(entries[30]["origin"], "weekly")
        self.assertTrue(entries[30]["segment_start"])

    def test_explicit_dated_transitions_are_explicit_not_inherited(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 30, rotation=self.D, on=MONDAY)
        entries = self.date_entries()
        self.assertEqual(entries[30]["explicit_block"].rotation.name, "D")
        self.assertFalse(entries[30]["inherited_transition"])
        self.assertEqual(entries[30]["origin"], "date_override")

    def test_inherited_is_only_ever_reported_where_the_effective_resolver_agrees(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 15, rotation=self.B, dow=0)
        make_row(self.profile, 10, 30, rotation=self.C, dow=0)
        make_row(self.profile, 10, 50, rotation=self.E, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        weekly, dated = load_hour_rows(self.profile, MONDAY, 10)
        starts = {s.start_minute: s for s in effective_segments(weekly, dated)}
        for entry in minute_map(weekly, dated, layer="date"):
            if entry["inherited_transition"]:
                self.assertIn(entry["minute"], starts)
                self.assertEqual(starts[entry["minute"]].origin, "weekly")
                self.assertEqual(starts[entry["minute"]].block.pk, entry["effective_block"].pk)
        # The weekly layer's own map is unaffected: nothing is ever "inherited" there.
        self.assertEqual(self.inherited(minute_map(weekly, [], layer="weekly")), [])

    def test_a_dated_row_at_the_base_covers_the_whole_hour_over_weekly_transitions(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 0, rotation=self.D, on=MONDAY)
        self.assertEqual(shape(self.segments()), [(0, "D", "date_override")])

    def test_a_later_dated_transition_supersedes_an_earlier_dated_transition(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        make_row(self.profile, 10, 50, rotation=self.E, on=MONDAY)
        self.assertEqual(
            shape(self.segments()), [(0, "A", "weekly"), (20, "D", "date_override"), (50, "E", "date_override")],
        )

    def test_reverting_an_exact_dated_transition_reveals_the_lower_layer(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        override = make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        self.assertEqual([s.start_minute for s in self.segments()], [0, 20])
        override.delete()
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly"), (30, "B", "weekly")])

    def test_an_hour_with_only_later_rows_is_not_buildable(self):
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 40, rotation=self.C, on=MONDAY)
        self.assertEqual(self.segments(), [])

    def test_a_dated_override_alone_is_a_valid_base_for_that_date(self):
        make_row(self.profile, 10, 0, rotation=self.D, on=MONDAY)
        make_row(self.profile, 10, 30, rotation=self.E, on=MONDAY)
        self.assertEqual(shape(self.segments()), [(0, "D", "date_override"), (30, "E", "date_override")])

    def test_rows_off_a_whole_minute_do_not_take_part(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        ScheduleBlock.objects.create(
            profile=self.profile, day_of_week=0, start_time=time(10, 30, 15),
            end_time=time(11, 0), rotation=self.B,
        )
        self.assertEqual(shape(self.segments()), [(0, "A", "weekly")])

    def test_other_hours_are_not_mixed_in(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 11, 15, rotation=self.B, dow=0)
        self.assertEqual(shape(self.segments(hour=10)), [(0, "A", "weekly")])
        self.assertEqual(self.segments(hour=11), [])

    def test_first_segment_is_always_the_legacy_exact_hour_row(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 0, rotation=self.D, on=NEXT_MONDAY)
        for day in (MONDAY, NEXT_MONDAY):
            segments = self.segments(target_date=day)
            self.assertEqual(
                segments[0].block.pk, resolve_schedule_block(day, 10, profile=self.profile).pk,
            )

    def test_legacy_resolver_keeps_exact_hh00_meaning(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 22, 0, rotation=self.C, dow=0)
        make_row(self.profile, 23, 30, rotation=self.D, dow=0)
        self.assertEqual(resolve_schedule_block(MONDAY, 10, profile=self.profile).rotation.name, "A")
        # A transition row never makes its hour "start" a block, and a block
        # that started earlier is never carried into a later hour.
        self.assertIsNone(resolve_schedule_block(MONDAY, 11, profile=self.profile))
        self.assertIsNone(resolve_schedule_block(MONDAY, 23, profile=self.profile))
        self.assertEqual(resolve_schedule_segments(MONDAY, 23, self.profile), [])

    def test_minute_map_marks_explicit_inherited_and_segment_starts(self):
        make_row(self.profile, 10, 0, rotation=self.A, dow=0)
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        make_row(self.profile, 10, 20, rotation=self.D, on=MONDAY)
        weekly, dated = load_hour_rows(self.profile, MONDAY, 10)
        entries = minute_map(weekly, dated, layer="date")
        self.assertEqual(len(entries), 60)
        self.assertIsNone(entries[0]["explicit_block"])
        self.assertTrue(entries[20]["explicit_block"] is not None and entries[20]["segment_start"])
        self.assertTrue(entries[0]["inherited_transition"])  # weekly base, before the first dated row
        self.assertFalse(entries[30]["inherited_transition"])  # weekly row shadowed by the active dated layer
        self.assertFalse(entries[30]["segment_start"])
        self.assertEqual({entry["origin"] for entry in entries}, {"weekly", "date_override"})
        weekly_view = minute_map(weekly, dated, layer="weekly")
        self.assertIsNotNone(weekly_view[30]["explicit_block"])
        self.assertIsNone(weekly_view[20]["explicit_block"])

    def test_orphan_rows_are_flagged_and_never_effective(self):
        make_row(self.profile, 10, 30, rotation=self.B, dow=0)
        weekly, dated = load_hour_rows(self.profile, MONDAY, 10)
        entries = minute_map(weekly, dated, layer="weekly")
        self.assertFalse(entries[30]["has_base"])
        self.assertTrue(entries[30]["orphan"])
        self.assertEqual({entry["origin"] for entry in entries}, {"none"})
        self.assertEqual(effective_segments(weekly, dated), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class MinuteApiTests(ResolverFixtures, ApiMixin, TestCase):
    def setUp(self):
        ApiMixin.setUp(self)
        self.build_resolver_fixtures(base_fixtures=False)

    def write(self, *, hour=10, minute=None, rotation=None, playlist=None, day_of_week=0, on=None, profile=None):
        payload = {"profile_uuid": str((profile or self.default).uuid), "hour": hour}
        if minute is not None:
            payload["minute"] = minute
        if on is not None:
            payload["specific_date"] = on.isoformat()
        else:
            payload["day_of_week"] = day_of_week
        if playlist is not None:
            payload["playlist_id"] = playlist.id
        else:
            payload["rotation_id"] = (rotation or self.A).id
        return self.post_json(reverse("library:api-schedule-list"), payload)

    def delete(self, block, *, on=None, profile=None):
        url = reverse("library:api-schedule-delete", args=[block.pk])
        url += f"?profile={(profile or self.default).uuid}"
        if on is not None:
            url += f"&date={on.isoformat()}"
        return self.client.delete(url)

    def test_omitted_minute_defaults_to_the_hour_start(self):
        response = self.write()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["start_minute"], 0)
        self.assertEqual(ScheduleBlock.objects.get().start_time, time(10, 0))

    def test_minute_bounds_are_accepted_and_invalid_values_rejected_without_rounding(self):
        self.assertEqual(self.write(minute=0).status_code, 200)
        self.assertEqual(self.write(minute=59).status_code, 200)
        for bad in (-1, 60, 30.5, "x", "3.5", True, [], {}):
            with self.subTest(minute=bad):
                payload = {"profile_uuid": str(self.default.uuid), "day_of_week": 0, "hour": 10,
                           "rotation_id": self.A.id, "minute": bad}
                response = self.post_json(reverse("library:api-schedule-list"), payload)
                self.assertEqual(response.status_code, 400, (bad, response.content))
        self.assertEqual(ScheduleBlock.objects.count(), 2)  # only :00 and :59

    def test_integer_strings_are_accepted_for_minute(self):
        self.write(minute=0)
        payload = {"profile_uuid": str(self.default.uuid), "day_of_week": 0, "hour": 10,
                   "rotation_id": self.B.id, "minute": "30"}
        self.assertEqual(self.post_json(reverse("library:api-schedule-list"), payload).status_code, 200)

    def test_multiple_transitions_in_one_hour_are_stored_as_exact_rows_only(self):
        for minute, rotation in ((0, self.A), (30, self.B), (45, self.A)):
            response = self.write(minute=minute, rotation=rotation)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["created"])
        self.assertEqual(
            list(ScheduleBlock.objects.order_by("start_time").values_list("start_time", flat=True)),
            [time(10, 0), time(10, 30), time(10, 45)],
        )
        listed = self.client.get(reverse("library:api-schedule-list") + f"?profile={self.default.uuid}").json()
        self.assertEqual([b["start_minute"] for b in listed["blocks"]], [0, 30, 45])
        self.assertEqual({b["start_hour"] for b in listed["blocks"]}, {10})

    def test_exact_minute_write_is_an_idempotent_upsert(self):
        self.write(minute=0)
        first = self.write(minute=30, rotation=self.B).json()
        second = self.write(minute=30, rotation=self.C).json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(ScheduleBlock.objects.get(pk=first["id"]).rotation, self.C)
        self.assertEqual(ScheduleBlock.objects.filter(profile=self.default, day_of_week=0).count(), 2)

    def test_minute_rows_get_an_end_time_at_the_end_of_the_wall_clock_hour(self):
        self.write(minute=0)
        self.write(minute=30, rotation=self.B)
        self.assertEqual(
            set(ScheduleBlock.objects.values_list("end_time", flat=True)), {time(11, 0)},
        )

    def test_a_weekly_transition_without_a_base_is_rejected(self):
        response = self.write(minute=30)
        self.assertEqual(response.status_code, 409)
        self.assertIn("base assignment", response.json()["error"])
        self.assertFalse(ScheduleBlock.objects.exists())

    def test_a_dated_transition_needs_an_effective_base_but_may_inherit_the_weekly_one(self):
        rejected = self.write(minute=30, on=MONDAY)
        self.assertEqual(rejected.status_code, 409)
        self.write(minute=0)  # weekly base for Mondays
        accepted = self.write(minute=30, rotation=self.D, on=MONDAY)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        # A date on a different weekday has no weekly base.
        self.assertEqual(self.write(minute=30, on=TUESDAY).status_code, 409)
        # ... but a dated base at HH:00 provides one.
        self.assertEqual(self.write(minute=0, on=TUESDAY).status_code, 200)
        self.assertEqual(self.write(minute=30, on=TUESDAY).status_code, 200)

    def test_deleting_a_weekly_base_with_dependent_transitions_is_rejected(self):
        base = ScheduleBlock.objects.get(pk=self.write(minute=0).json()["id"])
        self.write(minute=30, rotation=self.B)
        response = self.delete(base)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["blockers"])
        self.assertTrue(ScheduleBlock.objects.filter(pk=base.pk).exists())

    def test_base_can_be_deleted_once_its_transitions_are_removed(self):
        base = ScheduleBlock.objects.get(pk=self.write(minute=0).json()["id"])
        later = ScheduleBlock.objects.get(pk=self.write(minute=30, rotation=self.B).json()["id"])
        self.assertEqual(self.delete(later).json(), {"ok": True, "deleted": True})
        self.assertEqual(self.delete(base).json(), {"ok": True, "deleted": True})

    def test_deleting_a_weekly_base_that_strands_a_dated_transition_is_rejected(self):
        base = ScheduleBlock.objects.get(pk=self.write(minute=0).json()["id"])
        self.write(minute=30, rotation=self.D, on=MONDAY)
        response = self.delete(base)
        self.assertEqual(response.status_code, 409)
        self.assertIn("2027-03-01", " ".join(response.json()["blockers"]))

    def test_deleting_a_dated_base_is_allowed_when_a_weekly_base_remains(self):
        self.write(minute=0)
        dated_base = ScheduleBlock.objects.get(pk=self.write(minute=0, rotation=self.D, on=MONDAY).json()["id"])
        self.write(minute=30, rotation=self.E, on=MONDAY)
        self.assertEqual(self.delete(dated_base, on=MONDAY).json(), {"ok": True, "deleted": True})

    def test_deleting_a_dated_base_without_a_weekly_base_is_rejected_while_transitions_remain(self):
        dated_base = ScheduleBlock.objects.get(pk=self.write(minute=0, rotation=self.D, on=MONDAY).json()["id"])
        self.write(minute=30, rotation=self.E, on=MONDAY)
        self.assertEqual(self.delete(dated_base, on=MONDAY).status_code, 409)

    def test_revert_deletes_only_the_exact_dated_transition(self):
        weekly = ScheduleBlock.objects.get(pk=self.write(minute=0).json()["id"])
        weekly_later = ScheduleBlock.objects.get(pk=self.write(minute=30, rotation=self.B).json()["id"])
        d20 = ScheduleBlock.objects.get(pk=self.write(minute=20, rotation=self.D, on=MONDAY).json()["id"])
        d50 = ScheduleBlock.objects.get(pk=self.write(minute=50, rotation=self.E, on=MONDAY).json()["id"])
        other = make_row(self.other, 10, 20, rotation=self.D, on=MONDAY)
        self.assertEqual(self.delete(d20, on=MONDAY).json(), {"ok": True, "deleted": True})
        remaining = set(ScheduleBlock.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {weekly.pk, weekly_later.pk, d50.pk, other.pk})

    def test_delete_cannot_touch_another_profiles_row_or_the_wrong_date(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        theirs = make_row(self.other, 10, 30, rotation=self.B, dow=0)
        self.assertEqual(self.delete(theirs).json(), {"ok": True, "deleted": False})
        mine = make_row(self.default, 10, 30, rotation=self.C, on=MONDAY)
        self.assertEqual(self.delete(mine, on=TUESDAY).json(), {"ok": True, "deleted": False})
        self.assertTrue(ScheduleBlock.objects.filter(pk__in=[theirs.pk, mine.pk]).count() == 2)

    def test_archived_profiles_reject_minute_writes_and_deletes(self):
        make_row(self.other, 10, 0, rotation=self.A, dow=0)
        row = make_row(self.other, 10, 30, rotation=self.B, dow=0)
        ScheduleProfile.objects.filter(pk=self.other.pk).update(is_archived=True)
        self.assertEqual(self.write(minute=45, profile=self.other).status_code, 409)
        self.assertEqual(self.delete(row, profile=self.other).status_code, 409)
        self.assertEqual(ScheduleBlock.objects.filter(profile=self.other).count(), 2)

    def test_schedule_edit_is_required_for_minute_writes_and_deletes(self):
        row = make_row(self.default, 10, 30, rotation=self.B, dow=0)
        self.client.force_login(self.reader)
        self.assertEqual(self.write(minute=0).status_code, 403)
        self.assertEqual(self.delete(row).status_code, 403)
        self.assertTrue(ScheduleBlock.objects.filter(pk=row.pk).exists())

    def test_hour_detail_is_readable_without_the_edit_capability(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        self.client.force_login(self.reader)
        url = reverse("library:api-schedule-hour-detail") + f"?day_of_week=0&hour=10&profile={self.default.uuid}"
        self.assertEqual(self.client.get(url).status_code, 200)

    def hour_detail(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = self.client.get(reverse("library:api-schedule-hour-detail") + "?" + query)
        return response

    def test_weekly_hour_detail_reflects_the_layer_with_sixty_positions(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        make_row(self.default, 10, 30, rotation=self.B, dow=0)
        make_row(self.default, 10, 45, rotation=self.A, dow=0)
        data = self.hour_detail(day_of_week=0, hour=10, profile=self.default.uuid).json()
        self.assertEqual(data["mode"], "weekly")
        self.assertTrue(data["has_base"])
        self.assertEqual(len(data["minutes"]), 60)
        self.assertEqual([m["minute"] for m in data["minutes"]], list(range(60)))
        names = {m["minute"]: m["effective_block"]["content_name"] for m in data["minutes"]}
        self.assertEqual({names[0], names[29], names[45], names[59]}, {"A"})
        self.assertEqual({names[30], names[44]}, {"B"})
        explicit = [m["minute"] for m in data["minutes"] if m["explicit_block_id"]]
        self.assertEqual(explicit, [0, 30, 45])
        self.assertEqual([s["start_minute"] for s in data["segments"]], [0, 30, 45])

    def test_date_hour_detail_distinguishes_override_inherited_and_empty(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        make_row(self.default, 10, 30, rotation=self.B, dow=0)
        make_row(self.default, 10, 20, rotation=self.D, on=MONDAY)
        data = self.hour_detail(date=MONDAY.isoformat(), hour=10, profile=self.default.uuid).json()
        self.assertEqual(data["mode"], "date")
        by_minute = {m["minute"]: m for m in data["minutes"]}
        self.assertEqual(by_minute[5]["origin"], "weekly")
        self.assertEqual(by_minute[25]["origin"], "date_override")
        self.assertTrue(by_minute[20]["explicit_block_id"])
        # Weekly 10:30 B is shadowed by the active dated 10:20 D: never "inherited".
        self.assertFalse(by_minute[30]["inherited_transition"])
        self.assertEqual(by_minute[30]["effective_block"]["content_name"], "D")
        # The weekly 10:00 base precedes the first dated row and is inherited.
        self.assertTrue(by_minute[0]["inherited_transition"])
        self.assertEqual(by_minute[40]["effective_block"]["content_name"], "D")
        empty = self.hour_detail(date=MONDAY.isoformat(), hour=11, profile=self.default.uuid).json()
        self.assertFalse(empty["has_base"])
        self.assertEqual({m["origin"] for m in empty["minutes"]}, {"none"})

    def test_hour_detail_api_metadata_is_consistent_with_effective_segments(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        make_row(self.default, 10, 10, rotation=self.C, dow=0)
        make_row(self.default, 10, 30, rotation=self.B, dow=0)
        make_row(self.default, 10, 20, rotation=self.D, on=MONDAY)
        data = self.hour_detail(date=MONDAY.isoformat(), hour=10, profile=self.default.uuid).json()
        weekly, dated = load_hour_rows(self.default, MONDAY, 10)
        expected = effective_segments(weekly, dated)
        self.assertEqual(
            [(s["start_minute"], s["origin"], s["block"]["id"]) for s in data["segments"]],
            [(s.start_minute, s.origin, s.block.pk) for s in expected],
        )
        by_minute = {m["minute"]: m for m in data["minutes"]}
        self.assertEqual({m for m, e in by_minute.items() if e["segment_start"]}, {s.start_minute for s in expected})
        self.assertEqual({m for m, e in by_minute.items() if e["inherited_transition"]}, {0, 10})
        self.assertFalse(by_minute[30]["inherited_transition"])
        self.assertEqual(by_minute[30]["effective_block"]["content_name"], "D")
        self.assertEqual(by_minute[30]["origin"], "date_override")
        self.assertTrue(by_minute[20]["explicit_block_id"])
        self.assertFalse(by_minute[20]["inherited_transition"])

    def test_hour_detail_is_scoped_to_the_selected_profile_and_validates_input(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        theirs = self.hour_detail(day_of_week=0, hour=10, profile=self.other.uuid).json()
        self.assertFalse(theirs["has_base"])
        for params in ({"day_of_week": 9, "hour": 10}, {"day_of_week": 0, "hour": 24},
                       {"hour": 10}, {"date": "nope", "hour": 10}):
            with self.subTest(params=params):
                self.assertEqual(self.hour_detail(**params).status_code, 400)

    def test_date_overview_reports_server_derived_detail_counts(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        make_row(self.default, 10, 30, rotation=self.B, dow=0)
        make_row(self.default, 11, 0, rotation=self.C, dow=0)
        url = reverse("library:api-schedule-list") + f"?date={MONDAY.isoformat()}&profile={self.default.uuid}"
        cells = {c["hour"]: c for c in self.client.get(url).json()["cells"]}
        self.assertEqual(cells[10]["detail_count"], 1)
        self.assertEqual(cells[11]["detail_count"], 0)
        self.assertEqual(cells[10]["effective_block"]["content_name"], "A")  # base unchanged

    def test_existing_hourly_payload_shape_is_preserved_with_additive_minute_field(self):
        make_row(self.default, 7, 0, rotation=self.A, dow=2)
        listed = self.client.get(reverse("library:api-schedule-list") + f"?profile={self.default.uuid}").json()
        block = listed["blocks"][0]
        self.assertEqual(set(block), {
            "id", "day_of_week", "start_hour", "start_minute", "content_kind", "content_id", "content_name",
        })

    def test_cloning_a_profile_copies_minute_transitions_exactly(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        make_row(self.default, 10, 30, rotation=self.B, dow=0)
        make_row(self.default, 10, 45, rotation=self.A, dow=0)
        response = self.post_json(
            reverse("library:api-schedule-profile-action", args=[self.default.uuid, "clone"]),
            {"name": "Minute Clone"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        clone = ScheduleProfile.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(
            list(ScheduleBlock.objects.filter(profile=clone).order_by("start_time").values_list("start_time", flat=True)),
            [time(10, 0), time(10, 30), time(10, 45)],
        )
        self.assertEqual(shape(resolve_schedule_segments(MONDAY, 10, clone)), shape(resolve_schedule_segments(MONDAY, 10, self.default)))

    def test_profile_selection_and_state_remain_non_activating(self):
        make_row(self.default, 10, 0, rotation=self.A, dow=0)
        before = ScheduleProfileState.load().active_profile_id
        self.hour_detail(day_of_week=0, hour=10, profile=self.other.uuid)
        self.write(minute=0, profile=self.other)
        self.assertEqual(ScheduleProfileState.load().active_profile_id, before)


class BuilderFixtures:
    """Deterministic content: direct-track rotations and playlists with fixed
    airtime, so segment timing can be asserted exactly."""

    SECONDS = 300.0

    def build_builder_fixtures(self):
        ensure_schedule_profile_state()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        kind = CategoryKind.objects.create(code="minute-test", name="Minute Test")
        self.category = Category.objects.create(code="MINTEST", name="Minute Test", kind=kind)
        self.fill_category = Category.objects.create(code="MINFILL", name="Minute Fill", kind=kind)
        self.profile = ensure_schedule_profile_state().active_profile
        self._counter = 0

    def make_track(self, label, *, artist_name=None, category=None, seconds=None):
        self._counter += 1
        path = Path(self.tempdir.name) / f"{label}-{self._counter}.wav"
        path.touch()
        artist, _ = Artist.objects.get_or_create(name=artist_name or f"Artist {label} {self._counter}")
        seconds = self.SECONDS if seconds is None else seconds
        return Track.objects.create(
            filepath=str(path), filename=path.name, title=label, artist=artist,
            category=category or self.category, ready2air=True,
            duration_seconds=seconds, next_start_seconds=seconds,
        )

    def tracks(self, prefix, count, **kwargs):
        return [self.make_track(f"{prefix}{i + 1}", **kwargs) for i in range(count)]

    def direct_rotation(self, name, tracks):
        rotation = Rotation.objects.create(name=name)
        for position, track in enumerate(tracks):
            RotationSlot.objects.create(rotation=rotation, position=position, track=track)
        return rotation

    def playlist_of(self, name, tracks):
        playlist = Playlist.objects.create(name=name)
        for position, track in enumerate(tracks):
            PlaylistItem.objects.create(playlist=playlist, position=position, track=track)
        return playlist

    def schedule(self, hour, *entries, dow=0, on=None, profile=None):
        """entries: (minute, rotation_or_playlist)"""
        for minute, source in entries:
            make_row(
                profile or self.profile, hour, minute,
                rotation=source if isinstance(source, Rotation) else None,
                playlist=source if isinstance(source, Playlist) else None,
                dow=None if on else dow, on=on,
            )

    def titles(self, log):
        return [item.track_title for item in log.items.order_by("position")]


class CombinedHourBuilderTests(BuilderFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.build_builder_fixtures()
        self.a_tracks = self.tracks("A", 6)
        self.b_tracks = self.tracks("B", 6)
        self.rot_a = self.direct_rotation("Rot A", self.a_tracks)
        self.rot_b = self.direct_rotation("Rot B", self.b_tracks)

    def build(self, hour=10, **kwargs):
        return build_hour_log(MONDAY, hour, **kwargs)

    def test_a_detailed_hour_builds_one_combined_log_with_items_from_each_segment_in_order(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        log, error = self.build()
        self.assertIsNone(error)
        self.assertEqual(self.titles(log), [f"A{i}" for i in range(1, 7)] + [f"B{i}" for i in range(1, 7)])
        times = list(log.items.order_by("position").values_list("scheduled_time", flat=True))
        self.assertEqual(times, sorted(times))
        base = times[0]
        self.assertEqual([(t - base).total_seconds() for t in times], [300.0 * i for i in range(12)])

    def test_exactly_one_playlistlog_exists_for_the_hour_and_none_at_the_transitions(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b), (45, self.rot_a))
        log, _ = self.build()
        self.assertEqual(PlaylistLog.objects.count(), 1)
        self.assertEqual(
            list(PlaylistLog.objects.values_list("date", "hour")), [(MONDAY, 10)],
        )
        # Positions are a single contiguous sequence.
        self.assertEqual(
            list(log.items.order_by("position").values_list("position", flat=True)),
            list(range(log.items.count())),
        )

    def test_the_hour_is_persisted_exactly_once(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        with patch.object(log_builder, "_persist_log", wraps=log_builder._persist_log) as persist:
            self.build()
        self.assertEqual(persist.call_count, 1)

    def test_provenance_records_the_profile_and_the_profile_is_captured_once(self):
        other = ScheduleProfile.objects.create(name="Other")
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        self.schedule(10, (0, self.rot_b), (30, self.rot_a), profile=other)
        real = log_builder.get_active_schedule_profile
        calls = []

        def flip_after_first_read():
            result = real()
            calls.append(result)
            state = ScheduleProfileState.load()
            state.active_profile = other
            state.save()
            return result

        with patch.object(log_builder, "get_active_schedule_profile", side_effect=flip_after_first_read):
            log, error = self.build()
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(log.schedule_profile, self.profile)
        self.assertEqual(self.titles(log)[0], "A1")  # the captured profile's plan, not the flipped one

    def test_an_explicit_profile_builds_that_profiles_hour(self):
        other = ScheduleProfile.objects.create(name="Explicit")
        self.schedule(10, (0, self.rot_a), (30, self.rot_a))
        self.schedule(10, (0, self.rot_b), (30, self.rot_b), profile=other)
        log, _ = self.build(schedule_profile=other)
        self.assertEqual(log.schedule_profile, other)
        self.assertEqual(self.titles(log)[0], "B1")

    def test_approved_log_reuse_is_unchanged_and_schedule_edits_do_not_rewrite_it(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        built, error = build_and_approve_hour_log_locked(MONDAY, 10)
        self.assertIsNone(error)
        before = list(built.items.order_by("position").values_list("pk", "track_title"))
        # Edit the schedule after generation: add a 10:45 transition and drop 10:30.
        self.schedule(10, (45, self.rot_a))
        ScheduleBlock.objects.filter(profile=self.profile, start_time=time(10, 30)).delete()
        reused, error = build_and_approve_hour_log_locked(MONDAY, 10)
        self.assertIsNone(error)
        self.assertEqual(reused.pk, built.pk)
        built.refresh_from_db()
        self.assertEqual(built.status, "approved")
        self.assertEqual(built.schedule_profile, self.profile)
        self.assertEqual(list(built.items.order_by("position").values_list("pk", "track_title")), before)

    def test_editing_the_schedule_alone_never_touches_an_existing_log(self):
        self.schedule(10, (0, self.rot_a))
        built, _ = build_and_approve_hour_log_locked(MONDAY, 10)
        snapshot = list(built.items.order_by("position").values_list("pk", flat=True))
        self.schedule(10, (30, self.rot_b))
        built.refresh_from_db()
        self.assertEqual(built.status, "approved")
        self.assertEqual(list(built.items.order_by("position").values_list("pk", flat=True)), snapshot)
        self.assertEqual(PlaylistLog.objects.count(), 1)

    def test_admin_rebuild_replaces_the_hour_as_one_combined_draft(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        first, _ = build_hour_log_for_admin(MONDAY, 10)
        self.assertEqual(first.status, "draft")
        second, error = build_hour_log_for_admin(MONDAY, 10)
        self.assertIsNone(error)
        self.assertEqual(PlaylistLog.objects.filter(date=MONDAY, hour=10).count(), 1)
        self.assertEqual(second.status, "draft")
        self.assertEqual(self.titles(second), [f"A{i}" for i in range(1, 7)] + [f"B{i}" for i in range(1, 7)])

    def test_advisory_lock_remains_a_single_date_hour_authority(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        with patch.object(log_builder, "_advisory_lock_for_hour") as lock:
            lock.return_value.__enter__.return_value = True
            build_hour_log_for_admin(MONDAY, 10)
        lock.assert_called_once_with(MONDAY, 10)
        with patch.object(log_builder, "_advisory_lock_for_hour") as lock:
            lock.return_value.__enter__.return_value = False
            log, error = build_hour_log_for_admin(MONDAY, 10)
        self.assertEqual((log, error), (None, LOCK_CONTENDED))

    def test_a_single_segment_hour_is_equivalent_to_the_direct_rotation_build(self):
        # Rot A is only half an hour of direct tracks, so (exactly as before
        # 3.1C) fill_remaining_hour tops the hour up with random tracks. The
        # deterministic part -- the rotation itself, its order and its clock --
        # must be identical, as must the total hour length.
        self.schedule(10, (0, self.rot_a))
        via_schedule, _ = self.build()
        scheduled = [(i.track_title, i.scheduled_time) for i in via_schedule.items.order_by("position")]
        direct, _ = log_builder._build_from_rotation(MONDAY, 10, self.rot_a)
        direct_items = [(i.track_title, i.scheduled_time) for i in direct.items.order_by("position")]
        self.assertEqual(direct_items[:6], scheduled[:6])
        self.assertEqual([title for title, _ in scheduled[:6]], [f"A{i}" for i in range(1, 7)])
        self.assertEqual(len(direct_items), len(scheduled))
        self.assertEqual(PlaylistLog.objects.count(), 1)

    def test_rotation_then_playlist_and_playlist_then_rotation(self):
        playlist = self.playlist_of("Playlist P", self.tracks("P", 6))
        self.schedule(10, (0, self.rot_a), (30, playlist))
        log, error = self.build()
        self.assertIsNone(error)
        self.assertEqual(self.titles(log), [f"A{i}" for i in range(1, 7)] + [f"P{i}" for i in range(1, 7)])
        self.schedule(11, (0, playlist), (30, self.rot_b))
        log, error = self.build(hour=11)
        self.assertIsNone(error)
        self.assertEqual(self.titles(log), [f"P{i}" for i in range(1, 7)] + [f"B{i}" for i in range(1, 7)])

    def test_a_short_playlist_before_a_boundary_is_topped_up_not_truncated(self):
        fillers = self.tracks("F", 6, category=self.fill_category)
        config = LogFillConfig.load()
        config.strategy, config.fallback_category = "fixed_category", self.fill_category
        config.save()
        playlist = self.playlist_of("Short", self.tracks("S", 3))
        self.schedule(10, (0, playlist), (30, self.rot_b))
        preview, _ = preview_hour_log(MONDAY, 10)
        log, error = self.build()
        self.assertIsNone(error)
        titles = self.titles(log)
        self.assertEqual(titles[:3], ["S1", "S2", "S3"])
        self.assertEqual(
            {t for t in titles[3:6]} <= {f.title for f in fillers}, True,
        )
        self.assertEqual(titles[6:], [f"B{i}" for i in range(1, 7)])
        times = [i.scheduled_time for i in log.items.order_by("position")]
        self.assertEqual((times[6] - times[0]).total_seconds(), 1800.0)  # B lands on the boundary
        self.assertEqual([s["delay_seconds"] for s in preview["segments"]], [0.0, 0.0])

    def test_a_playlist_overrun_is_deterministic_never_truncated_and_delays_the_next_segment(self):
        long_playlist = self.playlist_of("Long", self.tracks("L", 4, seconds=600.0))  # 2400s past a 1800s boundary
        self.schedule(10, (0, long_playlist), (30, self.rot_b))
        log, error = self.build()
        self.assertIsNone(error)
        titles = self.titles(log)
        self.assertEqual(titles[:4], ["L1", "L2", "L3", "L4"])  # every explicit item kept
        self.assertEqual(titles[4:], ["B1", "B2", "B3", "B4"])  # B starts late and fills the hour
        preview, _ = preview_hour_log(MONDAY, 10)
        segments = preview["segments"]
        self.assertEqual(segments[1]["delay_seconds"], 600.0)
        self.assertEqual(segments[1]["actual_start_seconds"], 2400.0)
        self.assertTrue(any("starts 600s after its scheduled time" in i["message"] for i in preview["issues"]))
        # Deterministic: the same schedule yields the same combined hour again.
        again, _ = self.build()
        self.assertEqual(self.titles(again), titles)

    def test_an_overrun_that_consumes_a_whole_segment_window_never_skips_that_segment(self):
        long_playlist = self.playlist_of("Huge", self.tracks("H", 5, seconds=600.0))  # 3000s
        self.schedule(10, (0, long_playlist), (15, self.rot_a), (30, self.rot_b))
        log, _ = self.build()
        titles = self.titles(log)
        self.assertEqual(titles[:5], ["H1", "H2", "H3", "H4", "H5"])
        self.assertIn("A1", titles)  # the :15 segment still begins, delayed, contributing one track
        self.assertIn("B1", titles)
        self.assertLess(titles.index("A1"), titles.index("B1"))

    def test_preview_follows_the_same_segment_plan_as_the_real_build(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b), (45, self.rot_a))
        preview, error = preview_hour_log(MONDAY, 10)
        self.assertIsNone(error)
        self.assertEqual(PlaylistLog.objects.count(), 0)  # never persists
        self.assertEqual([s["start_time"] for s in preview["segments"]], ["10:00", "10:30", "10:45"])
        self.assertEqual(preview["source_name"], "Rot A → Rot B → Rot A")
        log, _ = self.build()
        self.assertEqual([i["title"] for i in preview["items"]], self.titles(log))
        self.assertEqual(preview["total_seconds"], 300.0 * log.items.count())

    def test_preview_of_a_single_source_hour_keeps_the_legacy_fields(self):
        self.schedule(10, (0, self.rot_a))
        preview, _ = preview_hour_log(MONDAY, 10)
        self.assertEqual((preview["source"], preview["source_name"]), ("rotation", "Rot A"))
        self.assertEqual(len(preview["segments"]), 1)

    def test_an_empty_source_fails_the_build_before_anything_is_persisted(self):
        empty = Rotation.objects.create(name="Empty Rot")
        self.schedule(10, (0, self.rot_a), (30, empty))
        log, error = self.build()
        self.assertIsNone(log)
        self.assertEqual(error, "Rotation 'Empty Rot' has no slots.")
        self.assertFalse(PlaylistLog.objects.exists())

    def test_a_late_start_skips_segments_whose_window_already_elapsed(self):
        self.schedule(10, (0, self.rot_a), (30, self.rot_b))
        # 1200s of the 3600s hour remain: the hour starts 40 minutes late, so
        # the 10:00-10:30 window ended before the real start.
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertIsNone(error)
        titles = self.titles(log)
        self.assertTrue(titles and all(t.startswith("B") for t in titles), titles)
        preview, _ = preview_hour_log(MONDAY, 10, target_duration_seconds=1200.0)
        self.assertTrue(preview["segments"][0]["elapsed_before_start"])
        self.assertFalse(preview["segments"][1]["elapsed_before_start"])

    def test_an_elapsed_empty_rotation_does_not_block_the_later_active_rotation(self):
        empty = Rotation.objects.create(name="Empty Rotation")
        self.schedule(10, (0, empty), (30, self.rot_b))
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertIsNone(error)
        titles = self.titles(log)
        self.assertTrue(titles and all(t.startswith("B") for t in titles), titles)
        self.assertEqual(PlaylistLog.objects.filter(date=MONDAY, hour=10).count(), 1)
        self.assertEqual(PlaylistLog.objects.count(), 1)

    def test_an_elapsed_empty_playlist_does_not_block_a_later_active_rotation_or_playlist(self):
        empty = Playlist.objects.create(name="Empty Playlist")
        self.schedule(10, (0, empty), (30, self.rot_b))
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertIsNone(error)
        self.assertTrue(all(t.startswith("B") for t in self.titles(log)))
        PlaylistLog.objects.all().delete()
        ScheduleBlock.objects.all().delete()
        later_playlist = self.playlist_of("Later Playlist", self.tracks("P", 2))
        self.schedule(10, (0, empty), (30, later_playlist))
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertIsNone(error)
        # The playlist leads; whatever follows is ordinary end-of-segment fill.
        self.assertEqual(self.titles(log)[:2], ["P1", "P2"])
        self.assertEqual(PlaylistLog.objects.count(), 1)

    def test_a_non_elapsed_empty_rotation_still_fails_the_real_build(self):
        empty = Rotation.objects.create(name="Empty Rotation")
        self.schedule(10, (0, empty), (30, self.rot_b))
        log, error = self.build()  # full hour: the 10:00 segment is active
        self.assertIsNone(log)
        self.assertEqual(error, "Rotation 'Empty Rotation' has no slots.")
        self.assertFalse(PlaylistLog.objects.exists())

    def test_a_non_elapsed_empty_playlist_still_fails_the_real_build(self):
        empty = Playlist.objects.create(name="Empty Playlist")
        self.schedule(10, (0, self.rot_a), (30, empty))
        log, error = self.build()
        self.assertIsNone(log)
        self.assertEqual(error, "Playlist 'Empty Playlist' has no items.")
        self.assertFalse(PlaylistLog.objects.exists())
        # The last (never-elapsed) segment is validated even in a late-start hour.
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertEqual((log, error), (None, "Playlist 'Empty Playlist' has no items."))
        self.assertFalse(PlaylistLog.objects.exists())

    def test_preview_does_not_report_an_elapsed_empty_source_as_an_error(self):
        for empty in (Rotation.objects.create(name="Empty Rotation"), Playlist.objects.create(name="Empty Playlist")):
            ScheduleBlock.objects.all().delete()
            self.schedule(10, (0, empty), (30, self.rot_b))
            preview, error = preview_hour_log(MONDAY, 10, target_duration_seconds=1200.0)
            self.assertIsNone(error)
            self.assertFalse(
                [i for i in preview["issues"] if "has no slots" in i["message"] or "has no items" in i["message"]],
                preview["issues"],
            )
            self.assertFalse([i for i in preview["issues"] if i["severity"] == "error"], preview["issues"])
            first, second = preview["segments"]
            self.assertTrue(first["elapsed_before_start"])
            self.assertEqual(first["items"], 0)
            self.assertFalse(second["elapsed_before_start"])
            self.assertTrue(preview["items"] and all(i["title"].startswith("B") for i in preview["items"]))
        self.assertFalse(PlaylistLog.objects.exists())

    def test_preview_reports_a_non_elapsed_empty_source(self):
        empty = Rotation.objects.create(name="Empty Rotation")
        self.schedule(10, (0, empty), (30, self.rot_b))
        preview, _ = preview_hour_log(MONDAY, 10)
        errors = [i["message"] for i in preview["issues"] if i["severity"] == "error"]
        self.assertIn("Rotation 'Empty Rotation' has no slots.", errors)
        self.assertFalse(preview["segments"][0]["elapsed_before_start"])
        self.assertFalse(PlaylistLog.objects.exists())

    def test_preview_and_real_build_agree_on_which_segments_are_applicable(self):
        empty = Rotation.objects.create(name="Empty Rotation")
        self.schedule(10, (0, empty), (30, self.rot_b))
        preview, _ = preview_hour_log(MONDAY, 10, target_duration_seconds=1200.0)
        log, error = self.build(target_duration_seconds=1200.0)
        self.assertIsNone(error)
        self.assertEqual([i["title"] for i in preview["items"]], self.titles(log))

    def test_no_prior_block_fallback_and_no_duplicate_log_in_a_continuation_hour(self):
        long_playlist = self.playlist_of("Evening Feature", self.tracks("E", 4, seconds=900.0))  # 3600s
        self.schedule(22, (0, long_playlist))
        built, _ = build_and_approve_hour_log_locked(MONDAY, 22)
        self.assertEqual(self.titles(built), ["E1", "E2", "E3", "E4"])
        # 23:00 is intentionally blank: nothing resolves, nothing is built.
        self.assertIsNone(resolve_schedule_block(MONDAY, 23, profile=self.profile))
        self.assertEqual(resolve_schedule_segments(MONDAY, 23, self.profile), [])
        log, error = build_hour_log(MONDAY, 23)
        self.assertEqual((log, error), (None, "No schedule block for this hour."))
        self.assertEqual(list(PlaylistLog.objects.values_list("hour", flat=True)), [22])

    def test_a_lone_minute_row_does_not_turn_a_blank_hour_into_a_buildable_one(self):
        self.schedule(23, (30, self.rot_a))
        self.assertIsNone(resolve_schedule_block(MONDAY, 23, profile=self.profile))
        log, error = build_hour_log(MONDAY, 23)
        self.assertEqual((log, error), (None, "No schedule block for this hour."))

    def test_the_next_real_scheduled_hour_still_builds_normally(self):
        self.schedule(22, (0, self.playlist_of("Feature", self.tracks("E", 4, seconds=900.0))))
        self.schedule(0, (0, self.rot_a), (30, self.rot_b))
        build_and_approve_hour_log_locked(MONDAY, 22)
        log, error = build_and_approve_hour_log_locked(MONDAY, 0)
        self.assertIsNone(error)
        self.assertEqual(len(self.titles(log)), 12)
        self.assertEqual(PlaylistLog.objects.count(), 2)


class CarriedSelectionStateTests(BuilderFixtures, TransactionTestCase):
    """Selection state crosses segment boundaries. Category slots are random,
    so each property is asserted over repeated builds of pools where a reset
    state would pick the excluded song about half the time."""

    REPEATS = 14
    HALF_HOUR = 1800.0

    def setUp(self):
        super().setUp()
        self.build_builder_fixtures()
        self.cat_one = Category.objects.create(code="ONE", name="One", kind=self.category.kind)
        self.cat_two = Category.objects.create(code="TWO", name="Two", kind=self.category.kind)

    def one_slot_rotation(self, name, category):
        rotation = Rotation.objects.create(name=name)
        RotationSlot.objects.create(rotation=rotation, position=0, category=category)
        return rotation

    def test_a_rotation_pick_in_an_earlier_segment_excludes_that_artist_in_a_later_one(self):
        self.make_track("X-song", artist_name="Artist X", category=self.cat_one, seconds=self.HALF_HOUR)
        self.make_track("X-other", artist_name="Artist X", category=self.cat_two, seconds=self.HALF_HOUR)
        self.make_track("Y-song", artist_name="Artist Y", category=self.cat_two, seconds=self.HALF_HOUR)
        first = self.one_slot_rotation("First", self.cat_one)
        second = self.one_slot_rotation("Second", self.cat_two)
        self.schedule(10, (0, first), (30, second))
        for _ in range(self.REPEATS):
            log, error = build_hour_log(MONDAY, 10)
            self.assertIsNone(error)
            self.assertEqual(self.titles(log), ["X-song", "Y-song"])

    def test_explicit_playlist_items_seed_exclusions_for_a_following_rotation(self):
        playlist = self.playlist_of("Seed", [
            self.make_track("X-playlist", artist_name="Artist X", category=self.cat_one, seconds=self.HALF_HOUR),
        ])
        self.make_track("X-rotation", artist_name="Artist X", category=self.cat_two, seconds=self.HALF_HOUR)
        self.make_track("Y-rotation", artist_name="Artist Y", category=self.cat_two, seconds=self.HALF_HOUR)
        rotation = self.one_slot_rotation("After Playlist", self.cat_two)
        self.schedule(10, (0, playlist), (30, rotation))
        for _ in range(self.REPEATS):
            log, error = build_hour_log(MONDAY, 10)
            self.assertIsNone(error)
            # The Playlist is never altered by recency; it only seeds exclusions.
            self.assertEqual(self.titles(log), ["X-playlist", "Y-rotation"])

    def test_a_track_used_in_one_segment_is_not_repeated_by_the_same_pool_in_the_next(self):
        self.make_track("Q-song", artist_name="Artist Q", category=self.cat_one, seconds=self.HALF_HOUR)
        self.make_track("R-song", artist_name="Artist R", category=self.cat_one, seconds=self.HALF_HOUR)
        rotation = self.one_slot_rotation("Shared Pool", self.cat_one)
        self.schedule(10, (0, rotation), (30, rotation))
        for _ in range(self.REPEATS):
            log, error = build_hour_log(MONDAY, 10)
            self.assertIsNone(error)
            self.assertEqual(sorted(self.titles(log)), ["Q-song", "R-song"])


@override_settings(SECURE_SSL_REDIRECT=False)
class HourDetailPageMarkupTests(ResolverFixtures, ApiMixin, TestCase):
    """Structural checks on the /schedule/ page; the browser behavior itself is
    covered by the Chromium smoke test."""

    def setUp(self):
        ApiMixin.setUp(self)
        self.build_resolver_fixtures(base_fixtures=False)
        self.html = self.client.get(reverse("library:schedule")).content.decode()

    def test_page_exposes_the_hour_detail_surface_for_both_modes(self):
        for marker in (
            'id="hourDetail"', 'id="minuteGrid"', 'role="group" aria-label="Minutes 00 to 59"',
            "function openHourDetail(", "function renderHourDetail(", "/api/schedule/hour-detail/",
            "Hour detail", "Hour Detail",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)
        self.assertEqual(self.html.count('id="hourDetail"'), 1)
        # Still ONE shared picker outside both mode containers (3.1B contract).
        self.assertEqual(self.html.count('id="contentPicker"'), 1)
        self.assertLess(self.html.index('id="contentPicker"'), self.html.index('id="weeklyDesktop"'))

    def test_detail_positions_come_from_the_server_response_not_client_precedence_logic(self):
        script = self.html[self.html.index("function renderHourDetail("):self.html.index("async function onMinuteClick(")]
        self.assertIn("data.minutes.forEach(", script)
        for server_field in ("entry.effective_block", "entry.explicit_block_id", "entry.inherited_transition", "entry.origin"):
            self.assertIn(server_field, script)
        # The script renders what it is given; it never walks rows to decide precedence.
        for forbidden in ("Math.max(", ".filter(", ".sort(", ".reduce("):
            self.assertNotIn(forbidden, script)

    def test_weekly_load_counts_minute_rows_as_detail_instead_of_overwriting_the_hour_cell(self):
        self.assertIn("if (block.start_minute) { detailCounts[block.day_of_week][block.start_hour] += 1; continue; }", self.html)

    def test_a_detailed_hour_has_a_distinct_neutral_overview_state(self):
        for marker in (".grid-cell.has-detail", ".mobile-hour-cell.has-detail", ".date-cell.has-detail",
                       "const DETAIL_COLOR = '#475569'", "cell.classList.toggle('has-detail', extra > 0)",
                       "detail-badge"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_writes_send_the_exact_minute_and_clearing_uses_only_the_explicit_row(self):
        self.assertIn("body = {profile_uuid: selectedProfile.uuid, hour: hourDetail.hour, minute: entry.minute}", self.html)
        self.assertIn("if (!entry.explicit_block_id)", self.html)
        self.assertIn("/api/schedule/${entry.explicit_block_id}/", self.html)

    def test_archived_profiles_cannot_write_from_hour_detail(self):
        self.assertIn("if (!hourDetail || !selectedProfile || selectedProfile.is_archived) return;", self.html)
        self.assertIn(".schedule-readonly .minute-cell", self.html)

    def test_hour_detail_closes_when_its_context_changes(self):
        self.assertIn("closeHourDetail();\n  scheduleMode = mode;", self.html)
        self.assertIn("closeHourDetail();\n    updateProfileChrome();", self.html)
        self.assertIn("addEventListener('change', () => { closeHourDetail(); loadDateSchedule(); })", self.html)

    def test_deferred_date_override_redesign_is_not_part_of_this_page(self):
        # The existing whole-day card grid is kept for 3.1C.
        self.assertIn('id="dateGrid" class="date-grid"', self.html)
