"""[P0] 1.21: playout continuity through the November 1, 2026 DST fall-back (America/Chicago).

The real engine's scheduler tick, queue rollover, current-hour loader, queue reload, playback claim and
authoritative air-start accounting run against PlaylistLog/LogItem/PlayEvent rows in the test database; only
GStreamer, deck starts and background builds are stubbed.  The clock is a controlled UTC instant
(``django.utils.timezone.now`` patched; the engine derives local time from it in TIME_ZONE America/Chicago), so
nothing depends on the host time zone or the real wall clock.  Media are empty temporary files.

This module imports only the engine and the models (no 1.21 helper), so the timeline test also runs unchanged
against r0130, where it reproduces the outage."""
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, time as dt_time, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogItem, PlayEvent, PlaylistLog, Track,
)
from library.services.engine import Deck, PlaybackEngine
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from monitoring.models import SystemEvent
from webrequests.models import SongRequest

UTC = dt_timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
NOV1 = date(2026, 11, 1)
MAR8 = date(2026, 3, 8)
NOV2 = date(2026, 11, 2)
GENERATED = LogItem.ORIGIN_GENERATED_SCHEDULE
ENGINE_INSERTED = LogItem.ORIGIN_ENGINE_INSERTED


def utc(hour, minute=0, second=0, day=NOV1):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def make_engine():
    engine = object.__new__(PlaybackEngine)
    engine.running = True
    engine.decks = {"A": None, "B": None}
    engine.manual_mode = False
    engine._lock = threading.RLock()
    engine._building_hours = set()
    engine.current_log = None
    engine.log_items = []
    engine._queue_cursor = 0
    engine._next_hour_peek = None
    engine._next_hour_peek_at = 0.0
    engine._next_hour_peek_key = None
    engine._last_queue_reload = 0
    engine._last_live_extend_attempt = 0.0
    engine._live_fill_in_progress = False
    engine._live_fill_generation = 0
    engine._forced_next_items = []
    engine._resume_item = None
    engine._resume_hint = None
    engine._start_next_track = MagicMock()
    engine._try_extend_live_log_async = MagicMock(return_value=False)
    engine._ensure_log_building = MagicMock(return_value="approved")
    engine._project_upcoming_hour_target_duration = MagicMock(return_value=eng_module.NOMINAL_HOUR_SECONDS)
    return engine


@override_settings(TIME_ZONE="America/Chicago", USE_TZ=True)
class _DstFixture(TransactionTestCase):
    def setUp(self):
        super().setUp()
        ensure_schedule_profile_state()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        kind = CategoryKind.objects.create(code="dst-music", name="DST Music")
        self.music = Category.objects.create(code="DSTMUSIC", name="DST Music", kind=kind)
        self.dedications, _ = Category.objects.get_or_create(code="Dedications",
                                                             defaults={"name": "Dedications", "kind": kind})
        self.artist = Artist.objects.create(name="DST Artist")
        self.serial = 0
        self.generation = 0

    @contextmanager
    def at(self, instant, engine=None):
        with patch("django.utils.timezone.now", return_value=instant):
            if engine is not None:
                engine._invalidate_schedule_authority("test clock moved")
                engine._last_queue_reload = 0
            yield

    def track(self, name):
        self.serial += 1
        path = Path(self.tempdir.name) / f"{name}-{self.serial}.wav"
        path.touch()
        return Track.objects.create(filepath=str(path), filename=path.name, title=name, artist=self.artist,
                                    category=self.music, ready2air=True, duration_seconds=900.0,
                                    next_start_seconds=895.0)

    def log(self, day, hour, names, *, start, step=timedelta(minutes=15), origin=GENERATED, category=None):
        log = PlaylistLog.objects.create(date=day, hour=hour, status="approved")
        for position, name in enumerate(names):
            track = self.track(name)
            LogItem.objects.create(playlist_log=log, position=position, scheduled_time=start + position * step,
                                   track=track, track_title=track.title, track_artist=self.artist.name,
                                   category=category or self.music, origin=origin)
        return log

    def rows(self, log):
        return list(LogItem.objects.filter(playlist_log=log).order_by("position"))

    def tick(self, engine):
        PlaybackEngine._ensure_upcoming_logs.__wrapped__(engine)

    def air(self, engine, item, instant):
        """The engine's real claim and first-real-buffer accounting for ``item`` at ``instant``."""
        self.generation += 1
        fresh = item.played_at is None
        deck = Deck(slot="A", track=item.track, log_item=item, pipeline=MagicMock(), mixer_pad=MagicMock(),
                    generation=self.generation, air_start_eligible=fresh,
                    continuation_reason=None if fresh else "already_started_occurrence")
        engine.decks["A"] = deck
        with self.at(instant):
            self.assertTrue(engine._claim_playback_occurrence(item))
            if fresh:
                engine._record_occurrence_air_start("A", self.generation, item.id, instant)
        engine.decks["A"] = None
        item.refresh_from_db()
        return item

    def next_item(self, engine, instant):
        with self.at(instant, engine):
            item, _forced = engine._next_queue_item()
        return item

    def play_through(self, engine, start, count, step=timedelta(minutes=15)):
        aired = []
        for index in range(count):
            item = self.next_item(engine, start + index * step)
            self.assertIsNotNone(item, f"nothing to play at {start + index * step}")
            aired.append(self.air(engine, item, start + index * step))
        return aired

    def replay_events(self, log):
        return SystemEvent.objects.filter(dedupe_key__startswith=f"engine|dst-fallback-replay|{log.id}|")


class FallBackTimelineTests(_DstFixture):
    def setUp(self):
        super().setUp()
        # 01:00 CDT (06:00 UTC) hour, its last item left unplayed by the early rollover.
        self.log01 = self.log(NOV1, 1, ["h1-a", "h1-b", "h1-c", "h1-d", "h1-e"], start=utc(6))
        # The approved 02:00 log: real 02:00 CST is 08:00 UTC.
        self.log02 = self.log(NOV1, 2, ["h2-a", "h2-b", "h2-c", "h2-d"], start=utc(8))

    def test_the_fall_back_timeline_restores_the_real_02_hour_and_counts_both_airings(self):
        engine = make_engine()
        with self.at(utc(6, 0, 5), engine):
            engine._load_log_for(NOV1, 1)
        self.play_through(engine, utc(6, 0, 5), 4)

        # 01:59:30 CDT: the established lookahead builds and installs the 02:00 log early. Not yet a replay.
        with self.at(utc(6, 59, 30), engine):
            self.tick(engine)
        self.assertEqual((engine.current_log.id, engine._queue_cursor), (self.log02.id, 0))
        self.assertFalse(LogItem.objects.filter(origin=ENGINE_INSERTED).exists())

        # The repeated 01:00 CST hour (07:00-08:00 UTC): the 02:00 programming supplies playout -- first pass.
        first = self.play_through(engine, utc(7, 0, 10), 4)
        first_played = {item.id: item.played_at for item in first}
        self.assertTrue(all(utc(7) <= at < utc(8) for at in first_played.values()))

        # 01:58 CST: exhausted. The active log is never re-installed as its own "next hour" (r0130 replayed its
        # first, already aired occurrence uncounted) and no pass is authorized before the bounded window opens.
        self.assertIsNone(self.next_item(engine, utc(7, 58)))
        engine._try_extend_live_log_async.assert_called()
        self.assertEqual(len(self.rows(self.log02)), 4)

        # 01:59:40 CST (window open: hour start 08:00 UTC minus the 30 s lookahead): one replay pass.
        with self.at(utc(7, 59, 40), engine):
            self.tick(engine)
        rows = self.rows(self.log02)
        originals, replay = rows[:4], rows[4:]
        self.assertEqual(len(replay), 4)
        self.assertEqual([(r.track_id, r.category_id, r.scheduled_time) for r in replay],
                         [(r.track_id, r.category_id, r.scheduled_time) for r in originals])
        self.assertTrue(all(r.origin == ENGINE_INSERTED and r.played_at is None and r.playback_claimed_at is None
                            for r in replay))
        self.assertEqual([r.position for r in replay], [4, 5, 6, 7])
        self.assertEqual(engine._queue_cursor, 4)
        engine._start_next_track.assert_called()                 # idle recovery resumes with the replay pass
        events = self.replay_events(self.log02)
        self.assertEqual(list(events.values_list("detail__outcome", flat=True)), ["authorized"])
        detail = events.get().detail
        self.assertEqual((detail["transition_utc"], detail["log_hour_start_utc"], detail["utc_offset_before"],
                          detail["utc_offset_after"]),
                         ("2026-11-01T07:00:00+00:00", "2026-11-01T08:00:00+00:00", "UTC-05:00", "UTC-06:00"))

        # Repeated ticks install nothing more.
        for instant in (utc(7, 59, 50), utc(8, 0, 0)):
            with self.at(instant, engine):
                self.tick(engine)
        self.assertEqual(len(self.rows(self.log02)), 8)
        self.assertEqual(self.replay_events(self.log02).count(), 1)

        # 02:00 CST: a fresh pass. A queue reload with the first replay occurrence on a deck keeps the cursor
        # after it -- it never jumps to the end because the first pass is played.
        item = self.next_item(engine, utc(8, 0, 10))
        self.assertEqual(item.id, replay[0].id)
        engine.decks["A"] = Deck(slot="A", track=item.track, log_item=item, pipeline=MagicMock(),
                                 mixer_pad=MagicMock(), generation=999)
        with self.at(utc(8, 0, 20), engine):
            engine._reload_queue_if_changed()
        self.assertEqual(engine._queue_cursor, 5)
        engine.decks["A"] = None
        self.air(engine, item, utc(8, 0, 10))
        second = [item] + self.play_through(engine, utc(8, 15), 3)
        self.assertEqual([r.id for r in second], [r.id for r in replay])

        # Exhausted again before 03:00 CST: never a third pass.
        self.assertIsNone(self.next_item(engine, utc(8, 55)))
        self.assertEqual(len(self.rows(self.log02)), 8)

        # 02:59:30 CST: the 03:00 log takes over by the ordinary lookahead.
        log03 = self.log(NOV1, 3, ["h3-a", "h3-b"], start=utc(9))
        with self.at(utc(8, 59, 30), engine):
            self.tick(engine)
        self.assertEqual((engine.current_log.id, engine._queue_cursor), (log03.id, 0))
        self.assertEqual(self.next_item(engine, utc(8, 59, 45)).playlist_log_id, log03.id)

        # Accounting: two truthful, distinguishable airings of each 02:00 occurrence; history untouched.
        for original in originals:
            original.refresh_from_db()
            self.assertEqual(original.played_at, first_played[original.id])
            self.assertEqual(Track.objects.get(pk=original.track_id).play_count, 2)
        snapshots = sorted(PlayEvent.objects.filter(log_item_id_snapshot__in=[r.id for r in rows])
                           .values_list("log_item_id_snapshot", flat=True))
        self.assertEqual(snapshots, sorted(r.id for r in rows))
        for occurrence in replay:
            occurrence.refresh_from_db()
            self.assertTrue(utc(8) <= occurrence.played_at < utc(9))
            event = PlayEvent.objects.get(log_item_id_snapshot=occurrence.id)
            self.assertEqual((event.source, event.started_at), ("scheduled", occurrence.played_at))

    def test_the_real_0200_cst_hour_is_not_silent(self):
        """The reported outage: the 02:00 programming consumed during the repeated hour left nothing to play from
        02:00 CST until the 03:00 lookahead (r0130: ``None`` here, i.e. silence)."""
        engine = make_engine()
        with self.at(utc(6, 59, 30), engine):
            engine._load_log_for(NOV1, 2)                        # installed early by the lookahead
        self.play_through(engine, utc(7, 0, 10), 4)              # consumed during the repeated hour
        with self.at(utc(8, 0, 10), engine):
            self.tick(engine)                                    # 02:00:10 CST scheduler tick
        item = self.next_item(engine, utc(8, 0, 15))
        self.assertIsNotNone(item, "the real 02:00 CST hour has nothing to play")
        self.assertEqual((item.playlist_log_id, item.origin, item.played_at), (self.log02.id, ENGINE_INSERTED, None))

    def test_a_first_pass_still_playing_at_the_window_continues_then_the_replay_follows(self):
        engine = make_engine()
        with self.at(utc(6, 59, 30), engine):
            engine._load_log_for(NOV1, 2)                        # the early install (cursor 0)
        self.play_through(engine, utc(7, 30), 2)                 # only half of the first pass aired early
        with self.at(utc(7, 59, 40), engine):
            self.tick(engine)
        rows = self.rows(self.log02)
        self.assertEqual(len(rows), 6)                           # only the two early airings are replayed
        self.assertEqual([r.track_id for r in rows[4:]], [r.track_id for r in rows[:2]])
        # The unplayed originals air first (once each), then the replay of the early ones.
        aired = self.play_through(engine, utc(7, 59, 45), 4, step=timedelta(minutes=10))
        self.assertEqual([r.id for r in aired], [rows[2].id, rows[3].id, rows[4].id, rows[5].id])
        self.assertIsNone(self.next_item(engine, utc(8, 50)))


class RestartAndRecoveryTests(_DstFixture):
    """Every restart re-derives the queue from the database; nothing replays more than authorized."""

    def setUp(self):
        super().setUp()
        self.log01 = self.log(NOV1, 1, ["h1-a", "h1-b", "h1-c"], start=utc(6), step=timedelta(minutes=20))
        self.log02 = self.log(NOV1, 2, ["h2-a", "h2-b", "h2-c"], start=utc(8), step=timedelta(minutes=20))

    def first_pass_aired(self, *, items=3):
        for index, row in enumerate(self.rows(self.log02)[:items]):
            LogItem.objects.filter(pk=row.pk).update(playback_claimed_at=utc(7, 20 * index),
                                                     played_at=utc(7, 20 * index, 5))

    def restarted(self, instant):
        engine = make_engine()
        with self.at(instant, engine):
            engine._load_current_hour_log()
        return engine

    def test_restart_before_or_during_the_first_0100_cdt_hour_is_ordinary(self):
        for instant in (utc(6, 59), utc(6, 30)):
            with self.subTest(instant=instant):
                engine = self.restarted(instant)
                self.assertEqual(engine.current_log.id, self.log01.id)
                with self.at(instant, engine):
                    self.tick(engine)
                self.assertFalse(LogItem.objects.filter(origin=ENGINE_INSERTED).exists())

    def test_restart_during_the_repeated_hour_never_restarts_the_aired_first_pass(self):
        self.first_pass_aired(items=2)
        for row in self.rows(self.log01):
            LogItem.objects.filter(pk=row.pk).update(played_at=utc(6, 10))
        engine = self.restarted(utc(7, 45))                      # wall clock (01:45 CST) resolves the 01 log
        self.assertEqual(engine.current_log.id, self.log01.id)
        item = self.next_item(engine, utc(7, 45, 5))             # 01 exhausted -> 02 at its first unplayed
        self.assertEqual(item.id, self.rows(self.log02)[2].id)
        self.assertFalse(LogItem.objects.filter(origin=ENGINE_INSERTED).exists())

    def test_restart_just_before_0200_cst_installs_the_replay_once(self):
        self.first_pass_aired()
        for row in self.rows(self.log01):
            LogItem.objects.filter(pk=row.pk).update(played_at=utc(6, 10))
        engine = self.restarted(utc(7, 59, 35))
        with self.at(utc(7, 59, 35), engine):
            self.tick(engine)                                    # lookahead advances to 02 (first unplayed)
            self.tick(engine)                                    # replay pass for the active 02 log
        rows = self.rows(self.log02)
        self.assertEqual(len(rows), 6)
        self.assertEqual(self.next_item(engine, utc(8, 0, 5)).id, rows[3].id)

    def test_restart_during_and_after_the_replay_pass_never_adds_a_third(self):
        self.first_pass_aired()
        engine = make_engine()
        with self.at(utc(8, 0, 5), engine):
            engine._load_log_for(NOV1, 2)
            self.tick(engine)
        rows = self.rows(self.log02)
        self.assertEqual(len(rows), 6)
        self.air(engine, self.next_item(engine, utc(8, 0, 10)), utc(8, 0, 10))

        partial = self.restarted(utc(8, 25))                     # partially progressed replay pass
        self.assertEqual(partial._queue_cursor, 4)
        with self.at(utc(8, 25), partial):
            self.tick(partial)
        self.assertEqual(len(self.rows(self.log02)), 6)
        self.assertEqual(list(self.replay_events(self.log02).values_list("detail__outcome", flat=True)
                              .order_by("id")), ["authorized", "already_handled"])
        for minute in (26, 40):
            self.air(partial, self.next_item(partial, utc(8, minute)), utc(8, minute))

        finished = self.restarted(utc(8, 50))                    # the replay pass has completed
        with self.at(utc(8, 50), finished):
            self.tick(finished)
        self.assertEqual(finished._queue_cursor, 6)
        self.assertIsNone(self.next_item(finished, utc(8, 50, 5)))
        self.assertEqual(len(self.rows(self.log02)), 6)

    def test_idle_recovery_and_manual_hold(self):
        self.first_pass_aired()
        engine = self.restarted(utc(8, 5))
        with self.at(utc(8, 5), engine):
            self.tick(engine)
        self.assertEqual(engine._queue_cursor, 3)
        engine._start_next_track.assert_called_once()

        held = self.restarted(utc(8, 6))
        held.manual_mode = True
        with self.at(utc(8, 6), held):
            self.tick(held)
        held._start_next_track.assert_not_called()               # a manual hold is never "recovered"
        self.assertEqual(len(self.rows(self.log02)), 6)

    def test_a_deck_spanning_the_window_keeps_its_place_ahead_of_the_replay(self):
        self.first_pass_aired(items=2)
        engine = make_engine()
        with self.at(utc(7, 59, 35), engine):
            engine._load_log_for(NOV1, 2)
        on_deck = self.rows(self.log02)[2]
        engine._queue_cursor = 3
        engine.decks["A"] = Deck(slot="A", track=on_deck.track, log_item=on_deck, pipeline=MagicMock(),
                                 mixer_pad=MagicMock(), generation=7)
        with self.at(utc(7, 59, 40), engine):
            self.tick(engine)
        self.assertEqual(engine._queue_cursor, 3)                 # after the on-deck item, at the first replay
        self.assertEqual(engine.log_items[3].origin, ENGINE_INSERTED)
        engine._start_next_track.assert_not_called()             # a playing deck is never doubled

    def test_a_busy_log_defers_and_a_later_tick_installs(self):
        self.first_pass_aired()
        engine = make_engine()
        with self.at(utc(8, 1), engine):
            engine._load_log_for(NOV1, 2)
        held, release = threading.Event(), threading.Event()

        def holder():
            try:
                with transaction.atomic():
                    PlaylistLog.objects.select_for_update().get(pk=self.log02.pk)
                    held.set()
                    release.wait(10)
            finally:
                close_old_connections()
                connection.close()

        worker = threading.Thread(target=holder, daemon=True)
        worker.start()
        self.assertTrue(held.wait(10))
        try:
            with self.at(utc(8, 1), engine):
                self.tick(engine)
            self.assertEqual(len(self.rows(self.log02)), 3)
        finally:
            release.set()
            worker.join(10)
        with self.at(utc(8, 1, 10), engine):
            self.tick(engine)
        self.assertEqual(len(self.rows(self.log02)), 6)


class AccountingExclusionTests(_DstFixture):
    def test_one_shot_and_late_occurrences_are_never_replayed_and_credit_is_not_duplicated(self):
        log02 = self.log(NOV1, 2, ["music", "request", "late", "never"], start=utc(8))
        music, request, late, never = self.rows(log02)
        dedication = LogItem.objects.create(playlist_log=log02, position=4, scheduled_time=utc(8, 50),
                                            track=self.track("dedication"), category=self.dedications,
                                            origin=GENERATED)
        urgent = LogItem.objects.create(playlist_log=log02, position=5, scheduled_time=utc(7, 33, 1),
                                        track=self.track("urgent"), category=self.music, origin=ENGINE_INSERTED)
        song_request = SongRequest.objects.create(external_request_id="dst-request", track=request.track,
                                                  log_item=request, status="scheduled", submitted_at=utc(6))
        engine = make_engine()
        with self.at(utc(7, 0), engine):
            engine._load_log_for(NOV1, 2)
        for row, instant in ((music, utc(7, 5)), (request, utc(7, 20)), (dedication, utc(7, 30)),
                             (urgent, utc(7, 34))):
            self.air(engine, row, instant)
        self.air(engine, late, utc(8, 2))
        with self.at(utc(8, 5), engine):
            engine._load_log_for(NOV1, 2)
            self.tick(engine)
        replay = [r for r in self.rows(log02) if r.position > 5]
        self.assertEqual([r.track_id for r in replay], [music.track_id])
        song_request.refresh_from_db()
        fulfilled_at = song_request.fulfilled_at
        self.assertEqual(song_request.status, "fulfilled")

        clone = replay[0]
        self.air(engine, clone, utc(8, 20))
        engine.decks["A"] = Deck(slot="A", track=clone.track, log_item=clone, pipeline=MagicMock(),
                                 mixer_pad=MagicMock(), generation=self.generation, air_start_eligible=True)
        with self.at(utc(8, 21)):
            engine._record_occurrence_air_start("A", self.generation, clone.id, utc(8, 21))   # a repeated callback
        engine.decks["A"] = None
        clone.refresh_from_db()
        self.assertEqual(clone.played_at, utc(8, 20))
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=clone.id).count(), 1)
        self.assertEqual(Track.objects.get(pk=music.track_id).play_count, 2)
        song_request.refresh_from_db()
        self.assertEqual(song_request.fulfilled_at, fulfilled_at)

    def test_claim_and_queue_installation_are_not_airings(self):
        log02 = self.log(NOV1, 2, ["a"], start=utc(8))
        (row,) = self.rows(log02)
        LogItem.objects.filter(pk=row.pk).update(playback_claimed_at=utc(7), played_at=utc(7, 0, 5))
        engine = make_engine()
        with self.at(utc(8, 0, 5), engine):
            engine._load_log_for(NOV1, 2)
            self.tick(engine)
        clone = self.rows(log02)[1]
        with self.at(utc(8, 0, 6)):
            self.assertTrue(engine._claim_playback_occurrence(clone))
        clone.refresh_from_db()
        self.assertIsNone(clone.played_at)
        self.assertFalse(PlayEvent.objects.filter(log_item_id_snapshot=clone.id).exists())
        self.assertEqual(Track.objects.get(pk=row.track_id).play_count, 0)


class SpringForwardAndNormalTimeTests(_DstFixture):
    def test_spring_forward_never_replays(self):
        # 2026-03-08: 02:00 CST does not exist (01:59:59 CST -> 03:00 CDT at 08:00 UTC).
        log01 = self.log(MAR8, 1, ["s1-a", "s1-b"], start=utc(7, day=MAR8), step=timedelta(minutes=30))
        log03 = self.log(MAR8, 3, ["s3-a", "s3-b"], start=utc(8, day=MAR8), step=timedelta(minutes=30))
        engine = make_engine()
        with self.at(utc(7, 0, 5, day=MAR8), engine):
            engine._load_log_for(MAR8, 1)
        self.play_through(engine, utc(7, 0, 5, day=MAR8), 2, step=timedelta(minutes=30))
        for instant in (utc(7, 59, 40, day=MAR8), utc(8, 0, 5, day=MAR8)):
            with self.at(instant, engine):
                self.tick(engine)
        self.assertEqual(engine.current_log.id, log03.id)
        self.play_through(engine, utc(8, 0, 10, day=MAR8), 2, step=timedelta(minutes=30))
        self.assertIsNone(self.next_item(engine, utc(8, 59, day=MAR8)))
        self.assertFalse(LogItem.objects.filter(origin=ENGINE_INSERTED).exists())
        self.assertFalse(SystemEvent.objects.filter(dedupe_key__startswith="engine|dst-fallback-replay|").exists())

    def test_an_ordinary_day_keeps_rollover_early_rollover_and_exhaustion_unchanged(self):
        # 2026-11-02 is CST (UTC-6): 13:00 local is 19:00 UTC.
        log13 = self.log(NOV2, 13, ["n13-a", "n13-b"], start=utc(19, day=NOV2), step=timedelta(minutes=30))
        log14 = self.log(NOV2, 14, ["n14-a", "n14-b"], start=utc(20, day=NOV2), step=timedelta(minutes=30))
        engine = make_engine()
        with self.at(utc(19, 0, 5, day=NOV2), engine):
            engine._load_log_for(NOV2, 13)
        self.play_through(engine, utc(19, 0, 5, day=NOV2), 2, step=timedelta(minutes=29))
        # Ordinary rollover on exhaustion installs the next hour at its top (cursor 0, as before).
        item = self.next_item(engine, utc(19, 59, day=NOV2))
        self.assertEqual((engine.current_log.id, item.id), (log14.id, self.rows(log14)[0].id))
        self.air(engine, item, utc(19, 59, 5, day=NOV2))
        self.air(engine, self.next_item(engine, utc(20, 20, day=NOV2)), utc(20, 20, day=NOV2))
        # Exhausted with no next hour: no replay of an ordinary hour, ever.
        self.assertIsNone(self.next_item(engine, utc(20, 50, day=NOV2)))
        with self.at(utc(20, 59, 40, day=NOV2), engine):
            self.tick(engine)
        restarted = make_engine()
        with self.at(utc(20, 59, 45, day=NOV2), restarted):
            restarted._load_current_hour_log()
            self.tick(restarted)
        self.assertEqual(restarted._queue_cursor, 2)
        self.assertFalse(LogItem.objects.filter(origin=ENGINE_INSERTED).exists())
        self.assertFalse(SystemEvent.objects.filter(dedupe_key__startswith="engine|dst-fallback-replay|").exists())
        # Early rollover by the lookahead still installs the next hour ahead of its top, at cursor 0.
        log15 = self.log(NOV2, 15, ["n15-a"], start=utc(21, day=NOV2))
        with self.at(utc(20, 59, 40, day=NOV2), engine):
            self.tick(engine)
        self.assertEqual((engine.current_log.id, engine._queue_cursor), (log15.id, 0))

    def test_a_rollover_never_installs_a_queue_with_nothing_unplayed(self):
        log01 = self.log(NOV1, 1, ["a"], start=utc(6))
        log02 = self.log(NOV1, 2, ["b", "c"], start=utc(8))
        LogItem.objects.filter(playlist_log__in=[log01, log02]).update(played_at=utc(6, 10))
        engine = make_engine()
        with self.at(utc(7, 30), engine):
            engine._load_log_for(NOV1, 1)
        # The peek offers the fully aired 02:00 log; the walk must end (live fill), never spin or hand it out.
        self.assertIsNone(self.next_item(engine, utc(7, 30, 5)))
        self.assertEqual(engine.current_log.id, log01.id)
        engine._try_extend_live_log_async.assert_called()

    def test_the_active_log_is_never_its_own_next_hour(self):
        log02 = self.log(NOV1, 2, ["a", "b"], start=utc(8))
        engine = make_engine()
        with self.at(utc(7, 30), engine):
            engine._load_log_for(NOV1, 2)
            self.assertIsNone(engine._peek_next_hour())
        other = make_engine()
        with self.at(utc(7, 30), other):
            self.assertEqual(other._peek_next_hour()[0].id, log02.id)   # not active here: a real candidate
