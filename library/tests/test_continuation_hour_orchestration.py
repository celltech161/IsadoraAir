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
from library.services.engine import PlaybackEngine
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
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state


FRIDAY = date(2027, 3, 5)
SATURDAY = date(2027, 3, 6)
THURSDAY = date(2027, 3, 4)


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
    obj._next_hour_peek_key = None
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
        # Blank-hour scenarios create no ScheduleBlock, yet the engine still
        # resolves against the active profile; a TransactionTestCase flush
        # removed the migration's profile state and production never recreates it.
        ensure_schedule_profile_state()
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

    def make_log(self, target_date, hour, *, tracks, played=False, scheduled_at=None):
        log = PlaylistLog.objects.create(
            date=target_date, hour=hour, status="approved"
        )
        items = []
        for position, track in enumerate(tracks):
            items.append(
                LogItem.objects.create(
                    playlist_log=log,
                    position=position,
                    scheduled_time=scheduled_at or timezone.make_aware(
                        timezone.datetime.combine(target_date, dt_time(hour, 0))
                    ),
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
        minute=0,
    ):
        return ScheduleBlock.objects.create(
            profile=ensure_schedule_profile_state().active_profile,
            specific_date=target_date if specific else None,
            day_of_week=None if specific else target_date.weekday(),
            start_time=dt_time(hour, minute),
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

    def partial_log(self, target_date, hour, minute, track):
        return self.make_log(
            target_date, hour, tracks=[track],
            scheduled_at=self.fake_now(target_date, hour, minute=minute),
        )

    def test_partial_next_hour_is_prebuilt_but_not_installed_or_peeked_early(self):
        old_track = self.make_track(duration=7200)
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, old_log, old_item)
        now = self.fake_now(FRIDAY, 10, minute=59)

        with patch.object(eng_module.timezone, "localtime", return_value=now):
            stand_in._advance_to_next_hour_log(FRIDAY, 11)
            peek = stand_in._peek_next_hour()

        self.assertEqual(stand_in.current_log.id, old_log.id)
        self.assertIsNone(peek)

    def test_partial_hour_before_takeover_is_intentional_continuation_without_warning(self):
        old_track = self.make_track(duration=7200)
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, old_log, old_item)
        now = self.fake_now(FRIDAY, 11, minute=10)

        emitted, thread = self.run_tick(stand_in, now)

        self.assertEqual(stand_in._current_hour_schedule_state(now)["state"], "partial_before_takeover")
        self.assertEqual(stand_in.current_log.id, old_log.id)
        self.assertEqual(emitted, [])
        thread.assert_not_called()

    def test_partial_takeover_switches_future_queue_without_touching_playing_deck(self):
        old_track = self.make_track(duration=7200)
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        partial, (partial_item,) = self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, old_log, old_item)
        deck_before = stand_in.decks["A"]

        emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 11, minute=30))

        self.assertEqual(stand_in.current_log.id, partial.id)
        self.assertEqual(stand_in.log_items[0].id, partial_item.id)
        self.assertEqual(stand_in._queue_cursor, 0)
        self.assertIs(stand_in.decks["A"], deck_before)
        self.assertEqual(emitted, [])
        thread.assert_not_called()

    # -- approved-log authority: edits after approval cannot move a takeover --
    def approved_partial_at_1130(self):
        """Old 10:00 track still on air; an 11:30 partial log built and
        approved from an 11:30 ScheduleBlock."""
        old_track = self.make_track(duration=7200)
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        block = self.make_block(FRIDAY, 11, minute=30)
        partial, (partial_item,) = self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, old_log, old_item)
        return stand_in, old_log, partial, partial_item, block

    def state_at(self, stand_in, minute, second=0):
        return stand_in._current_hour_schedule_state(self.fake_now(FRIDAY, 11, minute, second))

    def peek_at(self, stand_in, minute, second=0):
        stand_in._next_hour_peek = None  # never answer from the 5 s cache
        with patch.object(
            eng_module.timezone, "localtime",
            return_value=self.fake_now(FRIDAY, 11, minute, second),
        ):
            return stand_in._peek_next_hour()

    def assert_takes_over_exactly_at_1130(self, stand_in, old_log, partial, partial_item):
        before = self.state_at(stand_in, 29, 59)
        self.assertEqual(before["state"], "partial_before_takeover")
        self.assertEqual(before["authority"], "approved_log")
        self.assertEqual((before["takeover_time"].hour, before["takeover_time"].minute), (11, 30))
        self.assertIsNone(self.peek_at(stand_in, 29, 59))
        self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 29, 59))
        self.assertEqual(stand_in.current_log.id, old_log.id)

        due = self.state_at(stand_in, 30)
        self.assertEqual(due["state"], "partial_due")
        peek = self.peek_at(stand_in, 30)
        self.assertIsNotNone(peek, "crossfade look-ahead must see the approved partial log at 11:30")
        self.assertEqual(peek[0].id, partial.id)
        deck_before = stand_in.decks["A"]
        self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 30))
        self.assertEqual(stand_in.current_log.id, partial.id)
        self.assertEqual(stand_in.log_items[0].id, partial_item.id)
        self.assertIs(stand_in.decks["A"], deck_before)  # no hard cut

    def test_a_later_schedule_edit_does_not_delay_an_approved_partial_takeover(self):
        stand_in, old_log, partial, partial_item, block = self.approved_partial_at_1130()
        ScheduleBlock.objects.filter(pk=block.pk).update(start_time=dt_time(11, 45))
        self.assert_takes_over_exactly_at_1130(stand_in, old_log, partial, partial_item)

    def test_deleting_the_schedule_does_not_cancel_an_approved_partial_takeover(self):
        stand_in, old_log, partial, partial_item, _block = self.approved_partial_at_1130()
        ScheduleBlock.objects.filter(specific_date=FRIDAY).delete()
        self.assertFalse(ScheduleBlock.objects.filter(specific_date=FRIDAY).exists())
        self.assert_takes_over_exactly_at_1130(stand_in, old_log, partial, partial_item)

    def test_an_earlier_schedule_edit_cannot_advance_an_approved_partial_takeover(self):
        stand_in, old_log, partial, partial_item, block = self.approved_partial_at_1130()
        ScheduleBlock.objects.filter(pk=block.pk).update(start_time=dt_time(11, 15))
        at_quarter = self.state_at(stand_in, 15)
        self.assertEqual(at_quarter["state"], "partial_before_takeover")
        self.assertIsNone(self.peek_at(stand_in, 15))
        emitted, _thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 15))
        self.assertEqual(stand_in.current_log.id, old_log.id)
        self.assertEqual(emitted, [])
        self.assert_takes_over_exactly_at_1130(stand_in, old_log, partial, partial_item)

    def test_an_approved_ordinary_hour_is_not_reinterpreted_by_a_later_partial_edit(self):
        old_track = self.make_track()
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        block = self.make_block(FRIDAY, 11)  # ordinary 11:00
        ordinary, (first_item, _second) = self.make_log(
            FRIDAY, 11, tracks=[self.make_track(), self.make_track()],
        )
        ScheduleBlock.objects.filter(pk=block.pk).update(start_time=dt_time(11, 40))  # now "partial"
        stand_in = make_stand_in()
        stand_in.current_log = old_log
        stand_in.log_items = [old_item]
        stand_in._queue_cursor = 1  # old log exhausted, decks idle

        state = self.state_at(stand_in, 10)
        self.assertEqual(state["state"], "scheduled")
        self.assertEqual(state["authority"], "approved_log")
        self.assertTrue(state["takeover_due"])
        emitted, _thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 10))
        # Established ordinary behavior: the idle engine installs the hour's
        # approved log now rather than waiting for an 11:40 "takeover".
        self.assertEqual(stand_in.current_log.id, ordinary.id)
        self.assertEqual(stand_in.log_items[0].id, first_item.id)
        self.assertEqual(emitted, [])

    def test_before_materialization_the_live_schedule_still_classifies_the_hour(self):
        old_track = self.make_track(duration=7200)
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, old_log, old_item)

        state = self.state_at(stand_in, 10)
        self.assertEqual(state["authority"], "schedule")
        self.assertEqual(state["state"], "partial_before_takeover")
        self.assertTrue(state["has_hour_schedule"])
        _emitted, thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 10))
        thread.assert_called()  # the prospective partial hour is still built
        self.assertEqual(stand_in.current_log.id, old_log.id)

    # -- poison skips an occurrence; it never moves the approved boundary --
    def approved_log_with_items(self, hour, minutes):
        """An approved log for FRIDAY ``hour`` whose items are persisted at
        the given in-hour minutes (in order)."""
        tracks = [self.make_track() for _ in minutes]
        log, items = self.make_log(
            FRIDAY, hour, tracks=tracks,
            scheduled_at=self.fake_now(FRIDAY, hour, minute=minutes[0]),
        )
        for item, minute in zip(items, minutes):
            LogItem.objects.filter(pk=item.pk).update(
                scheduled_time=self.fake_now(FRIDAY, hour, minute=minute),
            )
        return log, list(log.items.order_by("position"))

    def idle_engine_on_exhausted_old_log(self):
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[self.make_track()], played=True)
        stand_in = make_stand_in()
        stand_in.current_log = old_log
        stand_in.log_items = [old_item]
        stand_in._queue_cursor = 1
        return stand_in, old_log

    def test_a_poisoned_first_occurrence_does_not_delay_a_partial_takeover(self):
        stand_in, old_log, _partial, _item, _block = self.approved_partial_at_1130()
        _partial.delete()
        partial, (first, second) = self.approved_log_with_items(11, (30, 34))
        stand_in._poison_skip_identities = [(partial.id, first.id)]

        before = self.state_at(stand_in, 29, 59)
        self.assertEqual(before["state"], "partial_before_takeover")
        self.assertEqual((before["takeover_time"].hour, before["takeover_time"].minute), (11, 30))
        due = self.state_at(stand_in, 30)
        self.assertEqual(due["state"], "partial_due")  # 11:30, not 11:34
        peek = self.peek_at(stand_in, 30)
        self.assertIsNotNone(peek)
        self.assertEqual([item.id for item in peek[1]], [second.id])

        deck_before = stand_in.decks["A"]
        self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 30))
        self.assertEqual(stand_in.current_log.id, partial.id)
        self.assertEqual([item.id for item in stand_in.log_items], [second.id])  # A skipped
        self.assertIs(stand_in.decks["A"], deck_before)
        selected, _forced = stand_in._next_queue_item()
        self.assertEqual(selected.id, second.id)

    def test_a_poisoned_first_occurrence_never_makes_an_ordinary_hour_partial(self):
        stand_in, _old_log = self.idle_engine_on_exhausted_old_log()
        ordinary, (first, second) = self.approved_log_with_items(11, (0, 4))
        stand_in._poison_skip_identities = [(ordinary.id, first.id)]

        state = self.state_at(stand_in, 2)
        self.assertEqual(state["state"], "scheduled")
        self.assertEqual(state["authority"], "approved_log")
        self.assertTrue(state["takeover_due"])
        self.assertIsNone(
            PlaybackEngine._log_queue_eligibility(ordinary, [second], self.fake_now(FRIDAY, 11, 2))[1],
            "an ordinary hour must not acquire an 11:04 partial boundary",
        )
        emitted, _thread = self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 2))
        self.assertEqual(stand_in.current_log.id, ordinary.id)
        self.assertEqual([item.id for item in stand_in.log_items], [second.id])
        # No scheduling warning. The one-time poison refusal is reported where
        # the item is actually refused -- queue materialization -- now that the
        # (r0103) cached authority no longer loads items at all.
        poison = [e for e in emitted if "poison" in e["title"].lower()]
        self.assertEqual([e for e in emitted if e not in poison], [])
        self.assertEqual([e["detail"]["log_item_id"] for e in poison], [first.id])
        self.assertEqual(poison[0]["detail"]["source"], "materialization")

    def test_an_all_poisoned_partial_log_keeps_its_boundary_and_replays_nothing(self):
        stand_in, old_log = self.idle_engine_on_exhausted_old_log()
        self.make_block(FRIDAY, 11, minute=30)
        partial, (first, second) = self.approved_log_with_items(11, (30, 34))
        stand_in._poison_skip_identities = [(partial.id, first.id), (partial.id, second.id)]

        # Before 11:30 it is still a partial hour -- never reinterpreted as an
        # ordinary/empty log that could replace the continuation early.
        early = self.state_at(stand_in, 10)
        self.assertEqual(early["state"], "partial_before_takeover")
        self.assertEqual((early["takeover_time"].hour, early["takeover_time"].minute), (11, 30))
        self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 10))
        self.assertEqual(stand_in.current_log.id, old_log.id)
        with patch.object(
            eng_module.timezone, "localtime", return_value=self.fake_now(FRIDAY, 11, 10),
        ), patch.object(eng_module, "GLib"):
            stand_in._on_log_exhausted("A")
        self.assertEqual(stand_in.current_log.id, old_log.id)
        stand_in._try_extend_live_log_async.assert_called_once()  # existing continuation policy
        self.assertEqual(self.state_at(stand_in, 29, 59)["state"], "partial_before_takeover")

        # At its own boundary it becomes due; nothing poisoned is ever offered.
        self.assertEqual(self.state_at(stand_in, 30)["state"], "partial_due")
        self.assertIsNone(self.peek_at(stand_in, 30))
        self.run_tick(stand_in, self.fake_now(FRIDAY, 11, 30))
        with patch.object(eng_module.timezone, "localtime", return_value=self.fake_now(FRIDAY, 11, 30)):
            stand_in._load_log_for(FRIDAY, 11)   # the exhaustion path's own load
        offered = {item.id for item in stand_in.log_items}
        self.assertFalse(offered & {first.id, second.id})

    def test_exhaustion_before_partial_takeover_uses_live_fill_not_partial_log(self):
        old_track = self.make_track()
        old_log, (old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()
        stand_in.current_log = old_log
        stand_in.log_items = [old_item]
        stand_in._queue_cursor = 1

        with patch.object(
            eng_module.timezone, "localtime",
            return_value=self.fake_now(FRIDAY, 11, minute=17),
        ), patch.object(eng_module, "GLib"):
            stand_in._on_log_exhausted("A")

        self.assertEqual(stand_in.current_log.id, old_log.id)
        stand_in._try_extend_live_log_async.assert_called_once()

    def test_missing_due_partial_log_never_skips_to_following_hour(self):
        old_log, (old_item,) = self.make_log(
            FRIDAY, 10, tracks=[self.make_track()], played=True,
        )
        self.make_block(FRIDAY, 11, minute=30)
        following, _ = self.make_log(FRIDAY, 12, tracks=[self.make_track()])
        stand_in = make_stand_in()
        stand_in.current_log = old_log
        stand_in.log_items = [old_item]
        stand_in._queue_cursor = 1

        with patch.object(
            eng_module.timezone, "localtime",
            return_value=self.fake_now(FRIDAY, 11, minute=30),
        ), patch.object(eng_module, "GLib") as glib:
            stand_in._on_log_exhausted("A")

        self.assertNotEqual(getattr(stand_in.current_log, "id", None), following.id)
        self.assertEqual(stand_in.log_items, [])
        glib.timeout_add_seconds.assert_called_once_with(30, stand_in._try_load_next_hour)

    def test_startup_before_partial_takeover_recovers_prior_same_day_queue(self):
        played = self.make_track()
        future = self.make_track()
        prior, items = self.make_log(FRIDAY, 10, tracks=[played, future])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        self.make_block(FRIDAY, 11, minute=30)
        self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone, "localtime",
            return_value=self.fake_now(FRIDAY, 11, minute=10),
        ):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, prior.id)
        self.assertEqual(stand_in._queue_cursor, 1)

    def test_startup_after_partial_takeover_loads_partial_queue(self):
        prior, _ = self.make_log(FRIDAY, 10, tracks=[self.make_track()], played=True)
        self.make_block(FRIDAY, 11, minute=30)
        partial, (partial_item,) = self.partial_log(FRIDAY, 11, 30, self.make_track())
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone, "localtime",
            return_value=self.fake_now(FRIDAY, 11, minute=35),
        ):
            stand_in._load_current_hour_log()

        self.assertNotEqual(prior.id, partial.id)
        self.assertEqual(stand_in.current_log.id, partial.id)
        self.assertEqual(stand_in.log_items[0].id, partial_item.id)

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

    def test_same_day_restart_loads_prior_log_and_resume_hint_installs_last_item(self):
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
        stand_in._install_resume_occurrence()

        # The interrupted last item is a DIRECT resume item; the queue itself
        # continues after it (nothing further in this log).
        self.assertEqual(stand_in._resume_item.id, items[-1].id)
        self.assertEqual(stand_in._queue_cursor, len(items))
        state = stand_in._current_hour_schedule_state(now)
        self.assertEqual(state["state"], "continuation")
        self.assertTrue(state["has_committed_playout"])

    def test_cross_midnight_startup_recovers_started_log_with_future_items(self):
        played = self.make_track()
        future = self.make_track()
        prior, items = self.make_log(FRIDAY, 23, tracks=[played, future])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        stand_in = make_stand_in()
        now = self.fake_now(SATURDAY, 0, minute=25)

        with patch.object(eng_module.timezone, "localtime", return_value=now):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, prior.id)
        self.assertEqual(stand_in._queue_cursor, 1)
        state = stand_in._current_hour_schedule_state(now)
        self.assertEqual(state["state"], "continuation")
        self.assertTrue(state["has_committed_playout"])

    def test_cross_midnight_startup_recovers_long_item_from_exact_resume_hint(self):
        long_track = self.make_track(duration=7200)
        prior, (item,) = self.make_log(
            FRIDAY, 23, tracks=[long_track], played=True
        )
        stand_in = make_stand_in()
        stand_in._resume_hint = {
            "track_id": long_track.id,
            "position": 4500.0,
            "log_item_id": item.id,
        }
        now = self.fake_now(SATURDAY, 0, minute=25)

        with patch.object(eng_module.timezone, "localtime", return_value=now):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, prior.id)
        self.assertEqual(stand_in._queue_cursor, 1)
        stand_in._install_resume_occurrence()
        self.assertEqual(stand_in._resume_item.id, item.id)
        self.assertEqual(stand_in._queue_cursor, 1)  # the queue resumes AFTER it
        self.assertEqual(stand_in.log_items[0].track_id, long_track.id)
        state = stand_in._current_hour_schedule_state(now)
        self.assertEqual(state["state"], "continuation")
        self.assertTrue(state["has_committed_playout"])

    def test_cross_midnight_startup_rejects_exhausted_yesterday_log(self):
        track = self.make_track()
        self.make_log(FRIDAY, 23, tracks=[track], played=True)
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)
        self.assertEqual(stand_in.log_items, [])

    def test_cross_midnight_startup_rejects_never_started_yesterday_log(self):
        track = self.make_track()
        self.make_log(FRIDAY, 23, tracks=[track])
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_current_schedule_block_prevents_cross_midnight_startup_fallback(self):
        self.make_block(SATURDAY, 0)
        played = self.make_track()
        future = self.make_track()
        _prior, items = self.make_log(FRIDAY, 23, tracks=[played, future])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_current_approved_log_wins_over_cross_midnight_candidate(self):
        played = self.make_track()
        future = self.make_track()
        _prior, prior_items = self.make_log(
            FRIDAY, 23, tracks=[played, future]
        )
        prior_items[0].played_at = timezone.now()
        prior_items[0].save(update_fields=["played_at"])
        current_track = self.make_track()
        current, _current_items = self.make_log(
            SATURDAY, 0, tracks=[current_track]
        )
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, current.id)
        self.assertEqual(stand_in.current_log.date, SATURDAY)

    def test_cross_midnight_startup_never_searches_back_two_days(self):
        played = self.make_track()
        future = self.make_track()
        _old, items = self.make_log(THURSDAY, 23, tracks=[played, future])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_cross_midnight_startup_does_not_skip_later_exhausted_log(self):
        played = self.make_track()
        future = self.make_track()
        _earlier, earlier_items = self.make_log(
            FRIDAY, 22, tracks=[played, future]
        )
        earlier_items[0].played_at = timezone.now()
        earlier_items[0].save(update_fields=["played_at"])
        latest_track = self.make_track()
        self.make_log(FRIDAY, 23, tracks=[latest_track], played=True)
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_cross_midnight_startup_rejects_unplayable_future_item(self):
        played = self.make_track()
        missing = self.make_track()
        _prior, items = self.make_log(FRIDAY, 23, tracks=[played, missing])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        Path(missing.filepath).unlink()
        stand_in = make_stand_in()

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_cross_midnight_startup_rejects_resume_hint_identity_mismatch(self):
        long_track = self.make_track(duration=7200)
        _prior, (item,) = self.make_log(
            FRIDAY, 23, tracks=[long_track], played=True
        )
        other_track = self.make_track()
        stand_in = make_stand_in()
        stand_in._resume_hint = {
            "track_id": other_track.id,
            "position": 4500.0,
            "log_item_id": item.id,
        }

        with patch.object(
            eng_module.timezone,
            "localtime",
            return_value=self.fake_now(SATURDAY, 0),
        ):
            stand_in._load_current_hour_log()

        self.assertIsNone(stand_in.current_log)

    def test_cross_midnight_startup_recovers_during_second_blank_hour(self):
        played = self.make_track()
        future = self.make_track(duration=7200)
        prior, items = self.make_log(FRIDAY, 23, tracks=[played, future])
        items[0].played_at = timezone.now()
        items[0].save(update_fields=["played_at"])
        stand_in = make_stand_in()
        now = self.fake_now(SATURDAY, 1, minute=15)

        with patch.object(eng_module.timezone, "localtime", return_value=now):
            stand_in._load_current_hour_log()

        self.assertEqual(stand_in.current_log.id, prior.id)
        self.assertEqual(stand_in._queue_cursor, 1)
        self.assertEqual(
            stand_in._current_hour_schedule_state(now)["state"],
            "continuation",
        )
