"""Roadmap 2.27 Phase A: LogItem.origin provenance.

Only writers that can prove an occurrence's origin set it; every other row
stays NULL (unknown), which the Eject evidence path never treats as
generated programming. Reorder and request swaps never change it."""
import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from library import views
from library.models import LogFillConfig, LogItem, PlaylistLog
from library.services import log_builder
from library.services.log_builder import append_fill_items, build_hour_log
from library.tests.test_schedule_minute_resolution_3_1c import MONDAY, BuilderFixtures
import library.services.engine as eng_module

GENERATED = LogItem.ORIGIN_GENERATED_SCHEDULE


class GeneratedHourOriginTests(BuilderFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.build_builder_fixtures()
        # 12 x 300 s fills the hour exactly, so no build-time fill is added.
        self.rotation = self.direct_rotation("Rot", self.tracks("A", 12))

    def origins(self, log):
        return list(log.items.order_by("position").values_list("track_title", "origin"))

    def test_schedule_generated_items_are_generated_schedule(self):
        self.schedule(10, (0, self.rotation))
        log, error = build_hour_log(MONDAY, 10)
        self.assertIsNone(error)
        self.assertEqual({origin for _title, origin in self.origins(log)}, {GENERATED})

    def test_build_time_fill_is_generated_fill_not_generated_schedule(self):
        fillers = self.tracks("F", 6, category=self.fill_category)
        config = LogFillConfig.load()
        config.strategy, config.fallback_category = "fixed_category", self.fill_category
        config.save()
        self.schedule(10, (0, self.playlist_of("Short", self.tracks("S", 3))))
        log, error = build_hour_log(MONDAY, 10)
        self.assertIsNone(error)
        filler_titles = {f.title for f in fillers}
        rows = self.origins(log)
        self.assertTrue(any(title in filler_titles for title, _ in rows))
        for title, origin in rows:
            expected = LogItem.ORIGIN_GENERATED_FILL if title in filler_titles else GENERATED
            self.assertEqual(origin, expected, title)

    def test_admin_rebuild_is_still_generated_schedule(self):
        self.schedule(10, (0, self.rotation))
        log, error = log_builder.build_hour_log_for_admin(MONDAY, 10)
        self.assertIsNone(error)
        self.assertEqual({o for _t, o in self.origins(log)}, {GENERATED})

    def test_play_now_playlist_build_stays_unknown(self):
        playlist = self.playlist_of("Now", self.tracks("N", 12))
        log, error = log_builder._build_from_playlist(MONDAY, 12, playlist)
        self.assertIsNone(error)
        self.assertEqual({o for _t, o in self.origins(log)}, {None})


class WriterOriginTests(BuilderFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.build_builder_fixtures()
        self.log = PlaylistLog.objects.create(date=MONDAY, hour=9, status="approved")
        self.generated = [
            LogItem.objects.create(
                playlist_log=self.log, position=i, scheduled_time=timezone.now(), track=track,
                track_title=track.title, category=self.category, origin=GENERATED,
            )
            for i, track in enumerate(self.tracks("G", 3))
        ]

    def test_legacy_and_unexamined_rows_default_to_unknown(self):
        legacy = LogItem.objects.create(
            playlist_log=self.log, position=50, scheduled_time=timezone.now(), category=self.category,
        )
        legacy.refresh_from_db()
        self.assertIsNone(legacy.origin)

    def test_live_fill_append_is_live_fill_even_for_fill_picks(self):
        track = self.make_track("L")
        picks = [{"position": 0, "scheduled_time": timezone.now(), "track": track,
                  "category": self.category, "origin": LogItem.ORIGIN_GENERATED_FILL}]
        (item,) = append_fill_items(self.log, picks, 10)
        item.refresh_from_db()
        self.assertEqual(item.origin, LogItem.ORIGIN_LIVE_FILL)

    def _post(self, view, body):
        request = RequestFactory().post("/x/", data=json.dumps(body), content_type="application/json")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=True, is_superuser=True, pk=1)
        items = list(self.log.items.order_by("position"))
        with patch.object(views, "authorize", return_value=True), \
                patch.object(views, "_read_engine_queue_state", return_value=(items, 1)):
            return view(request)

    def test_operator_insert_is_operator_inserted_and_stays_so_after_reposition(self):
        track = self.make_track("Inserted")
        response = self._post(views.api_engine_insert_track, {"track_id": track.id, "position": "next"})
        self.assertEqual(response.status_code, 200)
        item = LogItem.objects.get(pk=json.loads(response.content)["item_id"])
        self.assertEqual(item.position, 1)  # 9999 normalised by _reposition_items
        self.assertEqual(item.origin, LogItem.ORIGIN_OPERATOR_INSERTED)
        # The category comes from the Track; that does not make it generated.
        self.assertEqual(item.category_id, track.category_id)

    def test_generated_item_keeps_its_origin_when_reordered(self):
        moved = self.generated[2]
        response = self._post(views.api_engine_set_next, {"item_id": moved.id})
        self.assertEqual(response.status_code, 200)
        moved.refresh_from_db()
        self.assertEqual(moved.position, 1)
        self.assertEqual(moved.origin, GENERATED)
        self.assertEqual(set(self.log.items.values_list("origin", flat=True)), {GENERATED})

    def test_engine_splice_is_engine_inserted(self):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.current_log = self.log
        engine.log_items = list(self.log.items.order_by("position"))
        track = self.make_track("Urgent")
        new_item = engine._splice_log_item_db_at(1, track, self.category)
        new_item.refresh_from_db()
        self.assertEqual(new_item.origin, LogItem.ORIGIN_ENGINE_INSERTED)
        self.assertEqual(
            set(self.log.items.exclude(pk=new_item.pk).values_list("origin", flat=True)),
            {GENERATED},
        )

    def test_origin_is_not_editable_through_forms(self):
        self.assertFalse(LogItem._meta.get_field("origin").editable)
