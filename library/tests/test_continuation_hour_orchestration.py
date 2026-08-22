"""[P0] 1.1 intentional multi-hour/blank-hour orchestration regressions.

These tests exercise real ScheduleBlock resolution and PlaylistLog/LogItem
fixtures while keeping every GStreamer and background-worker side effect
mocked. Blank hours must never dispatch an impossible exact-start build;
whether they are healthy is decided separately from real committed playout.
"""

import tempfile
import threading
from datetime import date, time as dt_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist,
    Category,
    CategoryKind,
    LogItem,
    PlaylistLog,
    Rotation,
    ScheduleBlock,
    Track,
)


FRIDAY = date(2027, 3, 5)
SATURDAY = date(2027, 3, 6)


def make_stand_in():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._building_hours = set()
    obj.current_log = None
    obj.log_items = []
    obj._queue_cursor = 0
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    obj._last_live_extend_attempt = 0.0
    obj._live_fill_in_progress = False
    obj._live_fill_generation = 0
    obj._forced_next_items = []
    obj._start_next_track = MagicMock()
    obj._try_extend_live_log_async = MagicMock(return_value=False)
    obj._project_upcoming_hour_target_duration = MagicMock(
        return_value=eng_module.NOMINAL_HOUR_SECONDS
    )
    return obj


class ContinuationHourOrchestrationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        kind = CategoryKind.objects.create(
            code="continuation-test", name="Continuation Test"
        )
        self.category = Category.objects.create(
            code="CONTTEST", name="Continuation Test", kind=kind
        )
        self.artist = Artist.objects.create(name="Continuation Artist")
        self.rotation = Rotation.objects.create(name="Recurring Rotation")
        self.override_rotation = Rotation.objects.create(name="Specific Override")
        self._track_counter = 0

    def make_track(self, *, duration=3600.0):
        self._track_counter += 1
        path = Path(self.tempdir.name) / f"track-{self._track_counter}.wav"
        path.touch()
        return Track.objects.create(
            filepath=str(path),
            filename=path.name,
            title=f"Continuation Track {self._track_counter}",
            artist=self.artist,
            category=self.category,
            ready2air=True,
            duration_seconds=duration,
            next_start_seconds=duration,
        )

    def make_log(self, target_date, hour, *, tracks, played=False):
        log = PlaylistLog.objects.create(
            date=target_date, hour=hour, status="approved"
        )
        items = []
        for position, track in enumerate(tracks):
            items.append(
                LogItem.objects.create(
                    playlist_log=log,
                    position=position,
                    scheduled_time=timezone.now(),
                    track=track,
                    category=self.category,
                    played_at=timezone.now() if played else None,
                )
            )
        return log, items

    def make_block(
        self,
        target_date,
        hour,
        *,
        specific=True,
        rotation=None,
    ):
        return ScheduleBlock.objects.create(
            specific_date=target_date if specific else None,
            day_of_week=None if specific else target_date.weekday(),
            start_time=dt_time(hour, 0),
            end_time=dt_time((hour + 1) % 24, 0),
            rotation=rotation or self.rotation,
        )

    def fake_now(self, target_date, hour, minute=10, second=0):
        return timezone.make_aware(
            timezone.datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                minute,
                second,
            )
        )

    def run_tick(self, stand_in, now):
        emitted = []
        with patch.object(eng_module.timezone, "localtime", return_value=now), patch.object(
            eng_module,
            "emit_event",
            side_effect=lambda *args, **kwargs: emitted.append(kwargs),
        ), patch.object(eng_module, "GLib"), patch.object(
            eng_module.threading, "Thread"
        ) as thread:
            result = stand_in._ensure_upcoming_logs()
        self.assertTrue(result)
        return emitted, thread

    def put_last_item_on_deck(self, stand_in, log, item):
        stand_in.current_log = log
        stand_in.log_items = [item]
        stand_in._queue_cursor = 1
        stand_in.decks["A"] = SimpleNamespace(
            paused=False,
            finished=False,
            log_item=item,
            track=item.track,
        )

    def test_blank_hour_long_last_item_is_healthy_continuation(self):
        track = self.make_track(duration=7200)
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, log, item)

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 23))

        thread.assert_not_called()
        self.assertEqual(stand_in.current_log.id, log.id)
        self.assertEqual(stand_in._queue_cursor, len(stand_in.log_items))
        self.assertEqual(emitted, [])

    def test_blank_hour_future_items_are_committed_continuation(self):
        track = self.make_track()
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track])
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [item]

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 23))

        thread.assert_not_called()
        self.assertEqual(emitted, [])
        self.assertEqual(stand_in.current_log.id, log.id)
        stand_in._start_next_track.assert_called_once()

    def test_exhausted_prior_log_is_gap_not_continuation(self):
        track = self.make_track()
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [item]
        stand_in._queue_cursor = 1

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 23))

        thread.assert_not_called()
        self.assertEqual(
            [event["title"] for event in emitted],
            ["Unscheduled hour has no continuing program"],
        )
        self.assertEqual(
            emitted[0]["dedupe_key"],
            "engine|unscheduled-hour-gap|2027-03-05|23",
        )
        self.assertEqual(stand_in.current_log.id, log.id)
        stand_in._try_extend_live_log_async.assert_called_once()

    def test_natural_exhaustion_preserves_prior_log_while_live_fill_is_pending(self):
        track = self.make_track()
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [item]
        stand_in._queue_cursor = 1
        now = self.fake_now(FRIDAY, 23)

        with patch.object(eng_module.timezone, "localtime", return_value=now), patch.object(
            eng_module.GLib, "timeout_add_seconds"
        ) as timeout, patch.object(stand_in, "_load_log_for") as load:
            stand_in._on_log_exhausted("A")

        load.assert_not_called()
        self.assertEqual(stand_in.current_log.id, log.id)
        stand_in._try_extend_live_log_async.assert_called_once()
        timeout.assert_called_once_with(30, stand_in._try_load_next_hour)

    def test_blank_hour_retry_starts_fill_appended_to_prior_log(self):
        old_track = self.make_track()
        fill_track = self.make_track()
        log, (old_item,) = self.make_log(
            FRIDAY, 22, tracks=[old_track], played=True
        )
        fill_item = LogItem.objects.create(
            playlist_log=log,
            position=1,
            scheduled_time=timezone.now(),
            track=fill_track,
            category=self.category,
        )
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [old_item, fill_item]
        stand_in._queue_cursor = 1
        now = self.fake_now(FRIDAY, 23)

        with patch.object(eng_module.timezone, "localtime", return_value=now), patch.object(
            stand_in, "_load_log_for"
        ) as load:
            keep_polling = stand_in._try_load_next_hour()

        self.assertFalse(keep_polling)
        load.assert_not_called()
        self.assertEqual(stand_in.current_log.id, log.id)
        stand_in._start_next_track.assert_called_once()

    def test_retry_at_exact_scheduled_boundary_loads_new_hour_normally(self):
        self.make_block(SATURDAY, 0)
        track = self.make_track()
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [item]
        stand_in._queue_cursor = 1
        now = self.fake_now(SATURDAY, 0)

        def load_scheduled_hour(*_args):
            stand_in.current_log = None
            stand_in.log_items = []

        with patch.object(eng_module.timezone, "localtime", return_value=now), patch.object(
            stand_in, "_load_log_for", side_effect=load_scheduled_hour
        ) as load:
            keep_polling = stand_in._try_load_next_hour()

        self.assertTrue(keep_polling)
        load.assert_called_once_with(SATURDAY, 0)

    def test_blank_cold_gap_warns_without_dispatching_builder(self):
        stand_in = make_stand_in()

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 23))

        thread.assert_not_called()
        self.assertEqual(
            [event["title"] for event in emitted],
            ["Unscheduled hour has no continuing program"],
        )
        self.assertNotIn(
            "No approved log for current hour",
            [event["title"] for event in emitted],
        )

    def test_repeated_blank_hour_ticks_never_dispatch_doomed_worker(self):
        track = self.make_track()
        log, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, log, item)

        first_events, first_thread = self.run_tick(
            stand_in, self.fake_now(FRIDAY, 23, minute=10)
        )
        second_events, second_thread = self.run_tick(
            stand_in, self.fake_now(FRIDAY, 23, minute=20)
        )

        first_thread.assert_not_called()
        second_thread.assert_not_called()
        self.assertEqual(first_events + second_events, [])
        self.assertEqual(stand_in._building_hours, set())

    def test_real_current_hour_block_dispatches_normal_async_build(self):
        self.make_block(FRIDAY, 23)
        track = self.make_track()
        prior, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, prior, item)

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 23))

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertIn((FRIDAY, 23), stand_in._building_hours)
        self.assertIn(
            "Current hour's log not ready after rollover",
            [event["title"] for event in emitted],
        )

    def test_specific_date_current_block_wins_over_recurring_and_continuation(self):
        recurring = self.make_block(FRIDAY, 23, specific=False)
        specific = self.make_block(
            FRIDAY, 23, specific=True, rotation=self.override_rotation
        )
        track = self.make_track()
        prior, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, prior, item)

        state = stand_in._current_hour_schedule_state(self.fake_now(FRIDAY, 23))

        self.assertEqual(state["state"], "scheduled")
        self.assertEqual(state["schedule_block"].id, specific.id)
        self.assertNotEqual(state["schedule_block"].id, recurring.id)

    def test_midnight_lookahead_dispatches_saturday_schedule(self):
        self.make_block(SATURDAY, 0)
        track = self.make_track(duration=7200)
        prior, (item,) = self.make_log(FRIDAY, 22, tracks=[track], played=True)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, prior, item)
        stand_in._project_upcoming_hour_target_duration.return_value = 3300

        emitted, thread = self.run_tick(
            stand_in, self.fake_now(FRIDAY, 23, minute=59, second=35)
        )

        self.assertEqual(emitted, [])
        thread.assert_called_once()
        args = thread.call_args.kwargs["args"]
        self.assertEqual(args, (SATURDAY, 0, 3300))
        thread.return_value.start.assert_called_once()
        self.assertNotIn((FRIDAY, 23), stand_in._building_hours)
        self.assertIn((SATURDAY, 0), stand_in._building_hours)

    def test_approved_midnight_log_installs_after_friday_continuation(self):
        self.make_block(SATURDAY, 0)
        old_track = self.make_track(duration=7200)
        prior, (old_item,) = self.make_log(
            FRIDAY, 22, tracks=[old_track], played=True
        )
        next_track = self.make_track()
        midnight, _items = self.make_log(SATURDAY, 0, tracks=[next_track])
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, prior, old_item)

        emitted, thread = self.run_tick(
            stand_in, self.fake_now(FRIDAY, 23, minute=59, second=35)
        )

        thread.assert_not_called()
        self.assertEqual(emitted, [])
        self.assertEqual(stand_in.current_log.id, midnight.id)
        self.assertEqual(stand_in.current_log.date, SATURDAY)
        self.assertEqual(stand_in.current_log.hour, 0)

    def test_cross_date_early_rollover_remains_warning_free(self):
        track = self.make_track()
        midnight, (item,) = self.make_log(SATURDAY, 0, tracks=[track])
        stand_in = make_stand_in()
        stand_in.current_log = midnight
        stand_in.log_items = [item]

        emitted, thread = self.run_tick(
            stand_in, self.fake_now(FRIDAY, 23, minute=45)
        )

        thread.assert_not_called()
        self.assertEqual(emitted, [])
        self.assertEqual(stand_in.current_log.id, midnight.id)

    def test_same_day_restart_loads_prior_log_and_resume_hint_rewinds_last_item(self):
        first = self.make_track()
        last = self.make_track(duration=7200)
        prior, items = self.make_log(
            FRIDAY, 22, tracks=[first, last], played=True
        )
        stand_in = make_stand_in()
        now = self.fake_now(FRIDAY, 23, minute=25)

        with patch.object(eng_module.timezone, "localtime", return_value=now):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, prior.id)
        self.assertEqual(stand_in._queue_cursor, len(items))
        stand_in._resume_hint = {
            "track_id": last.id,
            "position": 5100.0,
            "log_item_id": items[-1].id,
        }
        stand_in._apply_resume_hint_queue_rewind()

        self.assertEqual(stand_in._queue_cursor, 1)
        state = stand_in._current_hour_schedule_state(now)
        self.assertEqual(state["state"], "continuation")
        self.assertTrue(state["has_committed_playout"])
