"""r0103 / 1.21 -- in-memory approved schedule authority for the 250 ms path.

Before: while a queue was exhausted (the continuation prefix before a partial
takeover, or an hour's last item), every 250 ms tick re-read schedule authority
twice (crossfade look-ahead + state-file preview): 4-8 ORM queries per tick.
Now the current hour's ScheduleAuthority record is held in memory and only
re-read on events or after SCHEDULE_AUTHORITY_REVALIDATE_SECONDS. The record
holds inputs that cannot change for a given approved log (its id and earliest
PERSISTED scheduled_time); eligibility is recomputed against the wall clock on
every call, so the cache can neither advance nor delay a takeover.
"""
import tempfile
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

import library.services.engine as eng_module
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Rotation, ScheduleBlock
from library.tests import test_continuation_hour_orchestration as continuation
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state

FRIDAY = continuation.FRIDAY
make_stand_in = continuation.make_stand_in


class ScheduleAuthorityCacheTests(TransactionTestCase):
    """Borrows the continuation suite's fixture helpers (functions only, so
    none of that suite's own tests are collected or run twice here)."""

    # Plain functions taken from the class; no module-level name refers to
    # that TestCase, so the loader never collects (and re-runs) its suite here.
    make_track = continuation.ContinuationHourOrchestrationTests.make_track
    make_log = continuation.ContinuationHourOrchestrationTests.make_log
    make_block = continuation.ContinuationHourOrchestrationTests.make_block
    fake_now = continuation.ContinuationHourOrchestrationTests.fake_now
    put_last_item_on_deck = continuation.ContinuationHourOrchestrationTests.put_last_item_on_deck

    def setUp(self):
        super().setUp()
        # Mirrors the continuation suite's setUp, which cannot be borrowed (zero-arg super).
        ensure_schedule_profile_state()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        kind = CategoryKind.objects.create(code="authority-test", name="Authority Test")
        self.category = Category.objects.create(code="AUTHTEST", name="Authority Test", kind=kind)
        self.artist = Artist.objects.create(name="Authority Artist")
        self.rotation = Rotation.objects.create(name="Authority Rotation")
        self.override_rotation = Rotation.objects.create(name="Authority Override")
        self._track_counter = 0
        old_track = self.make_track(duration=7200)
        self.old_log, (self.old_item,) = self.make_log(FRIDAY, 10, tracks=[old_track], played=True)

    def engine(self):
        stand_in = make_stand_in()
        self.put_last_item_on_deck(stand_in, self.old_log, self.old_item)
        return stand_in

    def at(self, minute, second=0):
        return self.fake_now(FRIDAY, 11, minute, second)

    def peek(self, stand_in, minute, second=0):
        with patch.object(eng_module.timezone, "localtime", return_value=self.at(minute, second)):
            return stand_in._peek_next_hour()

    def preview(self, stand_in, minute, second=0):
        with patch.object(eng_module.timezone, "localtime", return_value=self.at(minute, second)):
            return stand_in._get_upcoming_preview()

    def expire(self, stand_in):
        """Age the cached record past the revalidation bound."""
        record = stand_in._schedule_authority_cache
        stand_in._schedule_authority_cache = record.__class__(**{
            **record.__dict__,
            "loaded_monotonic": record.loaded_monotonic - eng_module.SCHEDULE_AUTHORITY_REVALIDATE_SECONDS - 1,
        })
        stand_in._next_hour_peek_at = 0.0

    def approved_partial(self, minute=30, *, minutes=None):
        self.make_block(FRIDAY, 11, minute=minute)
        log, items = self.make_log(
            FRIDAY, 11, tracks=[self.make_track() for _ in (minutes or (minute,))],
            scheduled_at=self.at(minute),
        )
        for item, m in zip(items, minutes or (minute,)):
            LogItem.objects.filter(pk=item.pk).update(scheduled_time=self.at(m))
        return log, list(log.items.order_by("position"))

    # 7 -- the point of the change -------------------------------------------
    def test_steady_state_polling_does_not_repeat_the_authority_query(self):
        self.approved_partial()
        stand_in = self.engine()
        self.peek(stand_in, 10)  # warm: one load
        with CaptureQueriesContext(connection) as ctx:
            for _ in range(40):  # ~10 s of 250 ms ticks, both poll consumers
                self.peek(stand_in, 10)
                self.preview(stand_in, 10)
        self.assertEqual(len(ctx.captured_queries), 0, [q["sql"] for q in ctx.captured_queries])

        self.expire(stand_in)
        with CaptureQueriesContext(connection) as ctx:
            self.peek(stand_in, 10)
            self.peek(stand_in, 10)
        authority_reads = [q for q in ctx.captured_queries if "MIN(" in q["sql"].upper()]
        self.assertEqual(len(authority_reads), 1, "exactly one bounded revalidation")

    def test_previously_uncached_negative_lookahead_is_now_bounded(self):
        # Ordinary hour on its last item, next hour not built: the look-ahead
        # used to re-run the next-hour log query on every call.
        ScheduleBlock.objects.all().delete()
        self.make_block(FRIDAY, 11)
        ordinary, _ = self.make_log(FRIDAY, 11, tracks=[self.make_track()])
        stand_in = make_stand_in()
        stand_in.current_log, stand_in.log_items, stand_in._queue_cursor = ordinary, [], 0
        self.assertIsNone(self.peek(stand_in, 50))
        with CaptureQueriesContext(connection) as ctx:
            for _ in range(40):
                self.assertIsNone(self.peek(stand_in, 50))
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_lookahead_cache_expiry_follows_monotonic_time_not_the_wall_clock(self):
        # Ordinary hour on its last item, next hour unbuilt: the look-ahead
        # caches "nothing approved" (the negative result 1.21 added).
        ScheduleBlock.objects.all().delete()
        self.make_block(FRIDAY, 11)
        ordinary, _ = self.make_log(FRIDAY, 11, tracks=[self.make_track()])
        stand_in = make_stand_in()
        stand_in.current_log, stand_in.log_items, stand_in._queue_cursor = ordinary, [], 0
        self.assertIsNone(self.peek(stand_in, 50))
        cached_at = stand_in._next_hour_peek_at
        real_monotonic = eng_module.time.monotonic

        def lookahead_reads(ctx):
            return [q for q in ctx.captured_queries if "MIN(" not in q["sql"].upper()]

        # A backward wall-clock step (NTP/manual correction) must not keep the
        # entry alive: only monotonic elapsed time decides expiry.
        bound = eng_module.SCHEDULE_AUTHORITY_REVALIDATE_SECONDS
        with patch.object(eng_module.time, "time", lambda: 0.0), \
                patch.object(eng_module.time, "monotonic", lambda: cached_at + bound + 0.5), \
                CaptureQueriesContext(connection) as ctx:
            self.assertIsNone(self.peek(stand_in, 50))
        self.assertEqual(len(lookahead_reads(ctx)), 1, "entry did not expire on monotonic time")
        self.assertEqual(stand_in._next_hour_peek_at, cached_at + bound + 0.5)

        # Conversely, a forward wall-clock jump alone does not expire it early.
        refreshed_at = stand_in._next_hour_peek_at
        with patch.object(eng_module.time, "time", lambda: real_monotonic() + 10 ** 9), \
                patch.object(eng_module.time, "monotonic", lambda: refreshed_at + 1.0), \
                CaptureQueriesContext(connection) as ctx:
            self.assertIsNone(self.peek(stand_in, 50))
        self.assertEqual(lookahead_reads(ctx), [], "a wall-clock jump expired the entry")
        self.assertEqual(stand_in._next_hour_peek_at, refreshed_at)

    # 3 -- the cache neither advances nor delays a takeover ------------------
    def test_cached_lookahead_crosses_the_approved_boundary_exactly(self):
        partial, (item,) = self.approved_partial()
        stand_in = self.engine()
        self.assertIsNone(self.peek(stand_in, 29, 50))
        with CaptureQueriesContext(connection) as ctx:
            self.assertIsNone(self.peek(stand_in, 29, 59))   # not advanced
            peek = self.peek(stand_in, 30, 0)                # not delayed
            self.assertIsNotNone(peek)
            self.assertEqual(peek[0].id, partial.id)
            for second in (1, 2, 3):
                self.assertEqual(self.peek(stand_in, 30, second)[0].id, partial.id)
        authority_reads = [q for q in ctx.captured_queries if "MIN(" in q["sql"].upper()]
        self.assertEqual(authority_reads, [], "boundary crossing must come from memory")
        # Only the takeover candidate's own queue is fetched, once, at 11:30.
        self.assertLessEqual(len(ctx.captured_queries), 2)

    def test_a_cached_ineligible_candidate_is_rechecked_on_every_call(self):
        # Ordinary 11:00 hour on its last item; the following hour has an
        # approved PARTIAL log at 12:30. The look-ahead caches that candidate
        # (ineligible when fetched) -- it must never return it from cache.
        ScheduleBlock.objects.all().delete()
        self.make_block(FRIDAY, 11)
        ordinary, _ = self.make_log(FRIDAY, 11, tracks=[self.make_track()])
        self.make_log(
            FRIDAY, 12, tracks=[self.make_track()],
            scheduled_at=self.fake_now(FRIDAY, 12, 30),
        )
        stand_in = make_stand_in()
        stand_in.current_log, stand_in.log_items, stand_in._queue_cursor = ordinary, [], 0
        self.assertIsNone(self.peek(stand_in, 59, 58))
        self.assertIsNotNone(stand_in._next_hour_peek)  # the candidate is cached...
        for second in (59,):
            self.assertIsNone(self.peek(stand_in, 59, second), "cached candidate offered before its takeover")

    # 1 -- immutability across ScheduleBlock edits, including revalidation ---
    def test_approved_boundary_survives_schedule_edits_and_revalidation(self):
        partial, _items = self.approved_partial()
        stand_in = self.engine()
        self.peek(stand_in, 10)
        for edited_minute in (15, 45):
            ScheduleBlock.objects.filter(specific_date=FRIDAY, start_time__hour=11).update(
                start_time=eng_module.datetime_time(11, edited_minute),
            )
            self.expire(stand_in)
            self.assertIsNone(self.peek(stand_in, 15), "an earlier edit advanced the takeover")
            self.assertIsNone(self.peek(stand_in, 29, 59))
            self.assertEqual(self.peek(stand_in, 30)[0].id, partial.id, "a later edit delayed it")
            record = stand_in._schedule_authority_cache
            self.assertEqual(record.source, "approved_log")
            self.assertEqual(record.first_scheduled, self.at(30))

    # 2 -- blank vs partial ---------------------------------------------------
    def test_blank_and_partial_hours_stay_distinct_through_the_cache(self):
        blank = self.engine()
        with patch.object(eng_module.timezone, "localtime", return_value=self.at(10)):
            blank_state = blank._current_hour_schedule_state(self.at(10), refresh=False)
        self.assertEqual(blank_state["authority"], "schedule")
        self.assertFalse(blank_state["has_hour_schedule"])
        self.assertEqual(blank_state["state"], "continuation")

        self.make_block(FRIDAY, 11, minute=30)  # partial, not yet materialized
        partial = self.engine()
        partial_state = partial._current_hour_schedule_state(self.at(10), refresh=False)
        self.assertEqual(partial_state["authority"], "schedule")
        self.assertTrue(partial_state["has_hour_schedule"])
        self.assertEqual(partial_state["state"], "partial_before_takeover")
        self.assertIsNone(self.peek(partial, 10))

    # 4 -- poison filtering cannot shift the boundary -------------------------
    def test_poison_cannot_shift_the_cached_boundary(self):
        partial, (first, second) = self.approved_partial(minutes=(30, 34))
        stand_in = self.engine()
        stand_in._poison_skip_identities = [(partial.id, first.id)]
        self.assertIsNone(self.peek(stand_in, 29, 59))
        peek = self.peek(stand_in, 30)
        self.assertEqual(stand_in._schedule_authority_cache.first_scheduled, self.at(30))
        self.assertEqual([item.id for item in peek[1]], [second.id])

    # 5 -- restart reconstructs an equivalent authority ----------------------
    def test_restart_reconstructs_an_equivalent_authority(self):
        self.approved_partial()
        records = []
        for _process in range(2):  # a fresh engine has no cache; it rebuilds from the DB
            stand_in = self.engine()
            self.assertIsNone(getattr(stand_in, "_schedule_authority_cache", None))
            self.peek(stand_in, 10)
            record = stand_in._schedule_authority_cache
            records.append({k: v for k, v in record.__dict__.items() if k != "loaded_monotonic"})
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0]["source"], "approved_log")

    # 6 -- reload when the approved log changes; fail-safe while stale --------
    def test_authority_reloads_when_the_approved_log_is_replaced(self):
        old_partial, _ = self.approved_partial(minute=30)
        stand_in = self.engine()
        self.peek(stand_in, 10)
        # Out of band (admin rebuild + approve, which do not signal the engine):
        # replaced by a log whose boundary is 11:40.
        PlaylistLog.objects.filter(pk=old_partial.pk).delete()
        new_log, _ = self.make_log(FRIDAY, 11, tracks=[self.make_track()], scheduled_at=self.at(40))

        # Still within the bound: the record is stale, but nothing can play
        # early -- the candidate's own immutable boundary still gates it.
        stand_in._next_hour_peek = None
        self.assertIsNone(self.peek(stand_in, 35), "stale authority let a log play before its boundary")

        self.expire(stand_in)
        self.assertIsNone(self.peek(stand_in, 35))
        self.assertEqual(stand_in._schedule_authority_cache.log_id, new_log.id)
        self.assertEqual(stand_in._schedule_authority_cache.first_scheduled, self.at(40))
        self.assertEqual(self.peek(stand_in, 40)[0].id, new_log.id)

    def test_explicit_invalidation_reloads_immediately(self):
        self.approved_partial()
        stand_in = self.engine()
        self.peek(stand_in, 10)
        self.assertIsNotNone(stand_in._schedule_authority_cache)
        stand_in._invalidate_schedule_authority("test")
        self.assertIsNone(stand_in._schedule_authority_cache)
        self.assertIsNone(stand_in._next_hour_peek)
        with CaptureQueriesContext(connection) as ctx:
            self.peek(stand_in, 10)
        self.assertEqual(len([q for q in ctx.captured_queries if "MIN(" in q["sql"].upper()]), 1)

    def test_reload_command_and_built_hour_install_invalidate(self):
        stand_in = self.engine()
        stand_in._invalidate_schedule_authority = calls = MagicMock()
        stand_in._reload_and_restart_current_log = lambda: None
        stand_in._dispatch_engine_command({"command": "reload_current_log"})
        calls.assert_called_with("reload_current_log")
        stand_in.running = True
        stand_in._advance_to_next_hour_log = lambda *a: None
        stand_in._install_built_hour(FRIDAY, 11)
        calls.assert_called_with("built hour installed")

    def test_event_driven_classification_still_reads_fresh(self):
        self.approved_partial()
        stand_in = self.engine()
        self.peek(stand_in, 10)
        with CaptureQueriesContext(connection) as ctx:
            stand_in._current_hour_schedule_state(self.at(10))  # default refresh=True
        self.assertEqual(len([q for q in ctx.captured_queries if "MIN(" in q["sql"].upper()]), 1)

    def test_cached_record_is_observable_without_a_database_read(self):
        self.approved_partial()
        stand_in = self.engine()
        self.peek(stand_in, 10)
        state = stand_in._schedule_authority_cache.as_state(eng_module.time.monotonic())
        self.assertEqual(state["source"], "approved_log")
        self.assertTrue(state["first_scheduled"].startswith("2027-03-05"))
        self.assertGreaterEqual(state["age_seconds"], 0)
