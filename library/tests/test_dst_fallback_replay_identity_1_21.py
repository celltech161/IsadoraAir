"""[P0] 1.21: the replay's single authorization and its PlayEvent accounting.

The authority is one marker on the approved PlaylistLog (``dst_replay_hour_start``: the real hour's UTC start),
set under the log's row lock in the same transaction as the ``dst_replay`` occurrences.  It is never inferred from
LogItems: foreign inserts, edited or deleted replay rows and duplicate originals can neither suppress the replay
nor authorize another.  A replay occurrence's PlayEvent is ``scheduled`` at any position.

Real PostgreSQL transactions (TransactionTestCase; the concurrency cases use separate connections), temporary media,
a controlled clock; no station resource."""
import csv
import io
import threading
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection, connections, transaction

from library.models import LogItem, PlayEvent, PlayEventSegment, PlaylistLog, Track
from library.services import dst_fallback, royalty_reports
from library.services.engine import Deck
from library.tests.test_dst_fallback_continuity_1_21 import (
    CHICAGO, NOV1, NOV2, _DstFixture, make_engine, utc,
)
from monitoring.models import SystemEvent

DST_REPLAY = LogItem.ORIGIN_DST_REPLAY
T = dst_fallback.fallback_successor(NOV1, 2, tz=CHICAGO)


class _Identity(_DstFixture):
    def setUp(self):
        super().setUp()
        self.log02 = self.log(NOV1, 2, ["a", "b", "c"], start=utc(8))
        self.originals = self.rows(self.log02)

    def first_pass(self, rows=None, at=utc(7, 10)):
        for index, row in enumerate(rows or self.originals):
            LogItem.objects.filter(pk=row.pk).update(playback_claimed_at=at + timedelta(minutes=10 * index),
                                                     played_at=at + timedelta(minutes=10 * index, seconds=5))

    def ensure(self, now=utc(8, 0, 5)):
        with self.at(now):
            return dst_fallback.ensure_replay(self.log02.id, T, now=now)

    def replay_rows(self):
        return list(LogItem.objects.filter(playlist_log=self.log02, origin=DST_REPLAY).order_by("position"))

    def marker(self):
        return PlaylistLog.objects.get(pk=self.log02.pk).dst_replay_hour_start

    def restart_ticks(self, minutes):
        for minute in minutes:
            engine = make_engine()                                # each a restart without any state snapshot
            with self.at(utc(8, minute, 30), engine):
                engine._load_current_hour_log()
                self.tick(engine)
                self.tick(engine)
        return engine

    def outcomes(self):
        return list(SystemEvent.objects.filter(dedupe_key__startswith=f"engine|dst-fallback-replay|{self.log02.id}|")
                    .values_list("detail__outcome", flat=True))


class ReplayAuthorizationTests(_Identity):
    def test_01_an_identical_foreign_engine_insert_does_not_suppress_the_replay(self):
        self.first_pass()
        a = self.originals[0]
        LogItem.objects.create(playlist_log=self.log02, position=3, scheduled_time=a.scheduled_time, track=a.track,
                               category=a.category, origin=LogItem.ORIGIN_ENGINE_INSERTED)
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.AUTHORIZED)
        self.assertEqual([r.track_id for r in self.replay_rows()], [r.track_id for r in self.originals])
        self.assertEqual([r.position for r in self.replay_rows()], [4, 5, 6])

    def test_02_duplicate_originals_are_each_replayed(self):
        a = self.originals[0]
        twin = LogItem.objects.create(playlist_log=self.log02, position=3, scheduled_time=a.scheduled_time,
                                      track=a.track, category=a.category, track_title=a.track_title,
                                      origin=LogItem.ORIGIN_GENERATED_SCHEDULE)
        self.first_pass(self.originals + [twin])
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)
        replay = self.replay_rows()
        self.assertEqual([(r.track_id, r.category_id, r.scheduled_time) for r in replay],
                         [(r.track_id, r.category_id, r.scheduled_time) for r in self.originals + [twin]])

    def test_03_the_marker_and_the_occurrences_commit_together(self):
        self.first_pass()
        generated_at = PlaylistLog.objects.get(pk=self.log02.pk).generated_at
        self.assertIsNone(self.marker())
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.AUTHORIZED)
        self.assertEqual(self.marker(), utc(8))                   # the real hour's UTC start, not a wall label
        self.assertEqual(result.replay_ids, [r.id for r in self.replay_rows()])
        self.assertTrue(all(r.played_at is None and r.playback_claimed_at is None for r in self.replay_rows()))
        self.assertEqual(PlaylistLog.objects.get(pk=self.log02.pk).generated_at, generated_at)   # only the marker
        self.assertFalse(PlaylistLog._meta.get_field("dst_replay_hour_start").editable)

    def test_04_nothing_aired_early_commits_no_marker(self):
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.NOT_ELIGIBLE)
        self.assertEqual((self.marker(), len(self.replay_rows())), (None, 0))
        self.first_pass()                                         # later evidence of an early airing still counts
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)

    def test_05_the_marker_decides_never_the_rows(self):
        self.first_pass()
        # A stray dst_replay row without the marker does not count as an authorization ...
        LogItem.objects.create(playlist_log=self.log02, position=3, scheduled_time=utc(8), track=self.originals[0].track,
                               category=self.originals[0].category, origin=DST_REPLAY)
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)
        self.assertEqual(len(self.replay_rows()), 4)
        # ... and a set marker with no dst_replay rows at all is a completed authorization.
        LogItem.objects.filter(playlist_log=self.log02, origin=DST_REPLAY).delete()
        self.assertEqual(self.ensure().outcome, dst_fallback.ALREADY_HANDLED)
        self.assertEqual(len(self.replay_rows()), 0)

    def test_06_edited_replay_rows_never_authorize_again(self):
        self.first_pass()
        self.ensure()
        other = self.track("other")
        edits = {"track": {"track": other}, "category": {"category": self.dedications},
                 "scheduled time": {"scheduled_time": utc(10)}}
        for (name, change), row in zip(edits.items(), self.replay_rows()):
            with self.subTest(edit=name):
                LogItem.objects.filter(pk=row.pk).update(**change)
                self.assertEqual(self.ensure().outcome, dst_fallback.ALREADY_HANDLED)
                self.assertEqual(len(self.replay_rows()), 3)
        LogItem.objects.filter(origin=DST_REPLAY).update(origin=LogItem.ORIGIN_OPERATOR_INSERTED)   # even the origin
        self.assertEqual(self.ensure().outcome, dst_fallback.ALREADY_HANDLED)
        self.assertEqual(LogItem.objects.filter(playlist_log=self.log02).count(), 6)

    def test_07_deleting_one_replay_row_never_authorizes_again(self):
        self.first_pass()
        self.ensure()
        self.replay_rows()[1].delete()
        self.assertEqual(self.ensure().outcome, dst_fallback.ALREADY_HANDLED)
        self.restart_ticks((2, 3))
        self.assertEqual(len(self.replay_rows()), 2)
        self.assertEqual(self.marker(), utc(8))

    def test_08_deleting_every_replay_row_never_authorizes_again(self):
        self.first_pass()
        self.ensure()
        LogItem.objects.filter(origin=DST_REPLAY).delete()
        self.assertEqual(self.ensure().outcome, dst_fallback.ALREADY_HANDLED)
        engine = self.restart_ticks((5,))                         # the operator's deletion is honoured on air too
        self.assertIsNone(self.next_item(engine, utc(8, 5, 35)))
        self.assertEqual((len(self.replay_rows()), self.marker()), (0, utc(8)))

    def test_09_repeated_ticks_and_restarts_authorize_once(self):
        self.first_pass()
        self.restart_ticks(range(5))
        self.assertEqual((len(self.replay_rows()), self.marker()), (3, utc(8)))
        self.assertEqual(self.outcomes(), ["authorized"])         # a restart that finds the marker emits nothing

    def test_10_two_concurrent_creators_commit_one_authorization(self):
        self.first_pass()
        barrier, out = threading.Barrier(2, timeout=10), {}

        def create(key):
            try:
                barrier.wait()
                out[key] = dst_fallback.ensure_replay(self.log02.id, T, now=utc(8, 0, 5)).outcome
            except BaseException as exc:
                out[key] = exc
            finally:
                connections.close_all()

        workers = [threading.Thread(target=create, args=(k,), daemon=True) for k in ("one", "two")]
        for w in workers:
            w.start()
        for w in workers:
            w.join(20)
        self.assertEqual(sorted(out), ["one", "two"])
        self.assertIn(dst_fallback.AUTHORIZED, out.values())
        self.assertTrue(set(out.values()) <= {dst_fallback.AUTHORIZED, dst_fallback.ALREADY_HANDLED, dst_fallback.BUSY},
                        out)
        self.assertEqual(list(out.values()).count(dst_fallback.AUTHORIZED), 1, out)
        self.assertEqual((len(self.replay_rows()), self.marker()), (3, utc(8)))

    def test_10b_a_creator_waiting_on_the_lock_sees_the_committed_marker(self):
        self.first_pass()
        held, release, out = threading.Event(), threading.Event(), {}

        def first():
            try:
                with transaction.atomic():
                    PlaylistLog.objects.select_for_update().get(pk=self.log02.pk)
                    held.set()
                    release.wait(10)
                    out["first"] = dst_fallback.ensure_replay(self.log02.id, T, now=utc(8, 0, 5)).outcome
            finally:
                connections.close_all()

        worker = threading.Thread(target=first, daemon=True)
        worker.start()
        self.assertTrue(held.wait(10))
        with patch.object(dst_fallback, "LOCK_TIMEOUT", "10s"):
            waiter = threading.Thread(
                target=lambda: (out.__setitem__("second", dst_fallback.ensure_replay(
                    self.log02.id, T, now=utc(8, 0, 6)).outcome), connections.close_all()), daemon=True)
            waiter.start()
            release.set()
            worker.join(15)
            waiter.join(15)
        self.assertEqual((out.get("first"), out.get("second")), (dst_fallback.AUTHORIZED, dst_fallback.ALREADY_HANDLED))
        self.assertEqual(len(self.replay_rows()), 3)

    def test_11_a_lock_timeout_commits_nothing_and_a_retry_succeeds(self):
        self.first_pass()
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
            self.assertEqual(self.ensure().outcome, dst_fallback.BUSY)
            self.assertEqual((self.marker(), len(self.replay_rows())), (None, 0))
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)

    def test_12_a_failure_partway_through_rolls_back_marker_and_rows(self):
        self.first_pass()
        real_create, calls = LogItem.objects.create, {"n": 0}

        def failing(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure after the first occurrence")
            return real_create(**kwargs)

        with patch.object(LogItem.objects, "create", side_effect=failing):
            result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.FAILED)
        self.assertEqual((self.marker(), len(self.replay_rows())), (None, 0))
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)

    def test_13_ordinary_engine_inserts_and_ordinary_days_are_unaffected(self):
        normal = self.log(NOV2, 13, ["n"], start=utc(19, day=NOV2))
        urgent = LogItem.objects.create(playlist_log=normal, position=1, scheduled_time=utc(19, 5, day=NOV2),
                                        track=self.track("urgent"), category=self.music,
                                        origin=LogItem.ORIGIN_ENGINE_INSERTED)
        engine = make_engine()
        with self.at(utc(19, 5, day=NOV2), engine):
            engine._load_log_for(NOV2, 13)
            self.tick(engine)
        self.air(engine, urgent, utc(19, 5, 10, day=NOV2))
        self.assertEqual(PlayEvent.objects.get(log_item_id_snapshot=urgent.id).source, "scheduled")  # position rule
        self.assertFalse(PlaylistLog.objects.filter(dst_replay_hour_start__isnull=False).exists())
        self.assertFalse(LogItem.objects.filter(origin=DST_REPLAY).exists())


class HighPositionAccountingTests(_Identity):
    def setUp(self):
        super().setUp()
        self.insert = LogItem.objects.create(playlist_log=self.log02, position=9999, scheduled_time=utc(7, 30),
                                             track=self.track("inserted"), category=self.music,
                                             origin=LogItem.ORIGIN_OPERATOR_INSERTED)

    def test_replay_airings_above_9999_are_scheduled_and_inserts_stay_insert(self):
        engine = make_engine()
        with self.at(utc(7), engine):
            engine._load_log_for(NOV1, 2)
        for row, at in zip(self.originals + [self.insert], (utc(7, 1), utc(7, 15), utc(7, 30), utc(7, 45))):
            self.air(engine, row, at)
        self.assertEqual(PlayEvent.objects.get(log_item_id_snapshot=self.insert.id).source, "insert")
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)
        replay = self.replay_rows()
        self.assertEqual([r.position for r in replay], [10000, 10001, 10002])
        self.assertEqual([r.track_id for r in replay], [r.track_id for r in self.originals])   # the insert: not replayed

        # A claim, a queue installation or a prepared deck is not an airing.
        with self.at(utc(8, 0, 5), engine):
            engine._load_log_for(NOV1, 2)
            self.assertTrue(engine._claim_playback_occurrence(replay[0]))
        self.assertFalse(PlayEvent.objects.filter(log_item_id_snapshot__in=[r.id for r in replay]).exists())

        for index, row in enumerate(replay):
            self.air(engine, row, utc(8, 1 + 15 * index))
        events = {e.log_item_id_snapshot: e for e in PlayEvent.objects.filter(log_item_id_snapshot__in=[r.id for r in replay])}
        self.assertEqual([events[r.id].source for r in replay], ["scheduled"] * 3)

        # A repeated callback and a continuation generation of a started replay occurrence add nothing.
        row = replay[0]
        row.refresh_from_db()
        for eligible, reason in ((True, None), (False, "auto_resume")):
            engine.decks["A"] = Deck(slot="A", track=row.track, log_item=row, pipeline=MagicMock(),
                                     mixer_pad=MagicMock(), generation=500, air_start_eligible=eligible,
                                     continuation_reason=reason)
            with self.at(utc(8, 3)):
                engine._record_occurrence_air_start("A", 500, row.id, utc(8, 3))
            engine.decks["A"] = None
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=row.id).count(), 1)

        # Counts: two airings for each original Track, one for the insert; duration evidence on its own occurrence.
        for original in self.originals:
            self.assertEqual(Track.objects.get(pk=original.track_id).play_count, 2)
        self.assertEqual(Track.objects.get(pk=self.insert.track_id).play_count, 1)
        for r in replay:
            segment = PlayEventSegment.objects.get(play_event=events[r.id])
            self.assertEqual(segment.log_item_id_snapshot, r.id)

        # The raw reporting export carries the same sources.
        content = royalty_reports.generate_raw_csv(date(2026, 11, 1), date(2026, 11, 1))
        text = content[0] if isinstance(content, tuple) else content
        rows = {int(r["log_item_id_snapshot"]): r["source"] for r in csv.DictReader(io.StringIO(text))
                if r["log_item_id_snapshot"]}
        self.assertEqual(rows[self.insert.id], "insert")
        self.assertEqual([rows[r.id] for r in replay], ["scheduled"] * 3)
        self.assertEqual([rows[r.id] for r in self.originals], ["scheduled"] * 3)
