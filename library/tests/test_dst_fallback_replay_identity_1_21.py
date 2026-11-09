"""[P0] 1.21 corrections (Codex review): durable replay identity and PlayEvent provenance.

BLOCKER 1 -- the replay pass is a database record (DstFallbackReplayPass, unique per log and physical successor
hour) committed with its occurrences; replay occurrences carry origin ``dst_replay``, their pass and the id of the
original they replay.  Foreign inserts, edited or deleted replay rows and duplicate originals can no longer suppress
the pass or authorize another.  BLOCKER 2 -- a replay occurrence's PlayEvent is ``scheduled`` at any position.

Real PostgreSQL transactions (TransactionTestCase; the concurrency cases use separate connections), temporary media,
a controlled clock; no station resource."""
import csv
import io
import threading
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.models import RestrictedError

from library.models import DstFallbackReplayPass, LogItem, PlayEvent, PlayEventSegment, PlaylistLog, Track
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
            return dst_fallback.ensure_replay(self.log02.id, T, now=now, reason="test")

    def replay_rows(self):
        return list(LogItem.objects.filter(playlist_log=self.log02, replay_pass__isnull=False).order_by("position"))

    def passes(self):
        return DstFallbackReplayPass.objects.filter(playlist_log=self.log02).count()


class ReplayIdentityTests(_Identity):
    def test_01_an_identical_foreign_engine_insert_does_not_suppress_the_replay(self):
        self.first_pass()
        a = self.originals[0]
        LogItem.objects.create(playlist_log=self.log02, position=3, scheduled_time=a.scheduled_time, track=a.track,
                               category=a.category, origin=LogItem.ORIGIN_ENGINE_INSERTED)
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.AUTHORIZED)
        self.assertEqual([r.replay_source_id_snapshot for r in self.replay_rows()], [r.id for r in self.originals])

    def test_02_duplicate_originals_are_each_replayed(self):
        a = self.originals[0]
        twin = LogItem.objects.create(playlist_log=self.log02, position=3, scheduled_time=a.scheduled_time,
                                      track=a.track, category=a.category, track_title=a.track_title,
                                      origin=LogItem.ORIGIN_GENERATED_SCHEDULE)
        self.first_pass(self.originals + [twin])
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)
        replay = self.replay_rows()
        self.assertEqual([r.replay_source_id_snapshot for r in replay], [r.id for r in self.originals] + [twin.id])
        self.assertEqual(len({(r.track_id, r.category_id, r.scheduled_time) for r in replay}), 3)   # twins kept
        self.assertEqual(DstFallbackReplayPass.objects.get().replay_count, 4)

    def test_03_to_05_edited_replay_rows_never_authorize_another_pass(self):
        self.first_pass()
        self.ensure()
        other = self.track("other")
        edits = {"track": {"track": other}, "category": {"category": self.dedications},
                 "scheduled time": {"scheduled_time": utc(10)}}
        for (name, change), row in zip(edits.items(), self.replay_rows()):
            with self.subTest(edit=name):
                LogItem.objects.filter(pk=row.pk).update(**change)
                result = self.ensure()
                self.assertIn(result.outcome, (dst_fallback.ALREADY_HANDLED, dst_fallback.DAMAGED))
                self.assertEqual((self.passes(), len(self.replay_rows())), (1, 3))
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.DAMAGED)
        self.assertEqual(len(result.changed_replay_ids), 2)                 # Track and Category edits are reported

    def test_06_deleting_one_replay_row_never_authorizes_another_pass(self):
        self.first_pass()
        self.ensure()
        gone = self.replay_rows()[1]
        gone.delete()
        result = self.ensure()
        self.assertEqual((result.outcome, result.missing_source_ids), (dst_fallback.DAMAGED, [self.originals[1].id]))
        self.assertEqual((self.passes(), len(self.replay_rows())), (1, 2))

    def test_07_deleting_every_replay_row_leaves_the_durable_pass(self):
        self.first_pass()
        self.ensure()
        LogItem.objects.filter(replay_pass__isnull=False).delete()
        result = self.ensure()
        self.assertEqual(result.outcome, dst_fallback.DAMAGED)
        self.assertEqual(result.missing_source_ids, [r.id for r in self.originals])
        self.assertEqual((self.passes(), len(self.replay_rows())), (1, 0))
        engine = make_engine()                                    # the operator's deletion is honoured on air too
        with self.at(utc(8, 5), engine):
            engine._load_current_hour_log()
            self.tick(engine)
        self.assertIsNone(self.next_item(engine, utc(8, 5, 5)))
        self.assertEqual(len(self.replay_rows()), 0)

    def test_08_and_09_repeated_ticks_and_restarts_never_add_a_pass(self):
        self.first_pass()
        for minute in range(5):
            engine = make_engine()                                # each a restart without any state snapshot
            with self.at(utc(8, minute, 30), engine):
                engine._load_current_hour_log()
                self.tick(engine)
                self.tick(engine)
        self.assertEqual((self.passes(), len(self.replay_rows())), (1, 3))
        outcomes = set(SystemEvent.objects.filter(dedupe_key__startswith=f"engine|dst-fallback-replay|{self.log02.id}|")
                       .values_list("detail__outcome", flat=True))
        self.assertEqual(outcomes, {"already_handled", "authorized"})

    def test_10_the_database_refuses_a_second_pass_for_the_same_log_and_hour(self):
        self.first_pass()
        self.ensure()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DstFallbackReplayPass.objects.create(
                playlist_log=self.log02, successor_hour_start=T.hour_start, transition_at=T.transition_at,
                time_zone=T.zone, authorized_at=utc(8, 1), source_log_item_ids=[], replay_count=0)

    def test_10b_two_concurrent_creators_commit_one_pass(self):
        self.first_pass()
        barrier, out = threading.Barrier(2, timeout=10), {}

        def create(key):
            try:
                barrier.wait()
                out[key] = dst_fallback.ensure_replay(self.log02.id, T, now=utc(8, 0, 5), reason=key).outcome
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
        self.assertEqual((self.passes(), len(self.replay_rows())), (1, 3))

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
            self.assertEqual((self.passes(), len(self.replay_rows())), (0, 0))
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(self.ensure().outcome, dst_fallback.AUTHORIZED)

    def test_12_a_failure_partway_through_rolls_back_the_whole_pass(self):
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
        self.assertEqual((self.passes(), len(self.replay_rows())), (0, 0))
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
        self.assertFalse(DstFallbackReplayPass.objects.exists())
        # The provenance is all-or-nothing in the database and only on a dst_replay row.
        self.first_pass()
        self.ensure()
        replay_pass = DstFallbackReplayPass.objects.get()
        for change in ({"replay_pass": replay_pass, "replay_source_id_snapshot": urgent.id},   # wrong origin
                       {"replay_source_id_snapshot": urgent.id},                               # no pass
                       {"replay_pass": replay_pass}):                                          # no source
            with self.subTest(change=sorted(change)), self.assertRaises(IntegrityError), transaction.atomic():
                LogItem.objects.filter(pk=urgent.pk).update(**change)

    def test_14_a_damaged_pass_is_reported_once_and_never_recreated(self):
        self.first_pass()
        self.ensure()
        self.replay_rows()[0].delete()
        for minute in (5, 6):
            engine = make_engine()
            with self.at(utc(8, minute), engine):
                engine._load_current_hour_log()
                self.tick(engine)
                self.tick(engine)
        damaged = SystemEvent.objects.filter(dedupe_key=f"engine|dst-fallback-replay|{self.log02.id}|damaged")
        self.assertTrue(damaged.exists())
        detail = damaged.first().detail
        self.assertEqual((detail["outcome"], detail["missing_source_log_item_ids"]),
                         ("damaged", [self.originals[0].id]))
        self.assertEqual((self.passes(), len(self.replay_rows())), (1, 2))

    def test_the_pass_lives_and_dies_with_its_log(self):
        self.first_pass()
        self.ensure()
        replay_pass = DstFallbackReplayPass.objects.get()
        with self.assertRaises(RestrictedError):
            replay_pass.delete()                                   # never while its occurrences exist
        self.log02.delete()                                       # the log takes both with it
        self.assertFalse(DstFallbackReplayPass.objects.exists())
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
        self.assertTrue(all(r.replay_source_id_snapshot != self.insert.id for r in replay))   # an insert: not replayed

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
