"""1.1 spec (2026-08-11), corrected in the second-pass review: async
live-fill worker replacing the synchronous _extend_current_log_live.

CRITICAL invariant under test: a stale worker result must be completely
side-effect-free. _live_fill_worker is READ-ONLY -- it computes a
PROPOSAL only and never touches PlaylistLog/LogItem. The DB write
(append_fill_items, transactional) happens ONLY in _install_live_fill,
and ONLY after that callback re-validates the proposal is still current
on the main thread (self.running, current_log id match, generation
match, queue-unchanged-since-dispatch, and a fresh wall-clock re-check).
Every "stale" test here asserts the DATABASE is unchanged, not just
self.log_items -- a worker that silently wrote to the DB before this
correction pass could pass an in-memory-only check while still
corrupting persisted state.

Same TransactionTestCase + bare-PlaybackEngine-stand-in pattern as
test_hour_log_async_build.py (real background threads need real commits
visible across connections -- see that file's own module docstring for
the full rationale)."""
import threading
import time
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogFillConfig, LogItem, PlaylistLog, Track,
)
from monitoring.models import SystemEvent


def make_stand_in(running=True):
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = running
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._live_fill_in_progress = False
    # Most _install_live_fill tests call with generation=1 (the first
    # real dispatch's number) -- default to matching that so each test
    # isolates its OWN staleness check rather than incidentally also
    # tripping the generation check. test_successful_dispatch_* and the
    # dedicated superseded-generation test override this explicitly.
    obj._live_fill_generation = 1
    obj._last_live_extend_attempt = 0.0
    obj.current_log = None
    obj.log_items = []
    obj._queue_cursor = 0
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    obj._start_next_track = MagicMock()
    return obj


class LiveFillFixtureMixin:
    def setUp(self):
        super().setUp()
        kind = CategoryKind.objects.create(code="livefilltest-kind", name="Live Fill Test Kind")
        self.category = Category.objects.create(code="LIVEFILLTEST", name="Live Fill Test", kind=kind)
        artist, _ = Artist.get_or_create_ci("Live Fill Test Artist")
        for i in range(5):
            Track.objects.create(
                filepath=f"/srv/isadoraair/music/livefill{i}.flac", filename=f"livefill{i}.flac",
                title=f"Live Fill {i}", artist=artist, category=self.category,
                ready2air=True, duration_seconds=180.0, next_start_seconds=175.0,
            )
        LogFillConfig.objects.update_or_create(pk=1, defaults={"strategy": "repeat_last_category"})

    def make_log_with_one_item(self, hour):
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=hour).delete()
        log = PlaylistLog.objects.create(date=today, hour=hour, status="approved")
        item = LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now(),
            track=Track.objects.filter(category=self.category).first(), category=self.category,
        )
        return log, [item]

    def make_proposal_picks(self, count=1):
        track = Track.objects.filter(category=self.category).first()
        return [
            {"track": track, "category": self.category, "scheduled_time": timezone.now()}
            for _ in range(count)
        ]


class DispatchGuardTests(LiveFillFixtureMixin, TransactionTestCase):
    def test_no_current_log_does_not_dispatch(self):
        stand_in = make_stand_in()
        with patch.object(eng_module.threading, "Thread") as mock_thread:
            result = stand_in._try_extend_live_log_async()
        self.assertFalse(result)
        mock_thread.assert_not_called()
        self.assertFalse(stand_in._live_fill_in_progress)

    def test_already_in_progress_does_not_dispatch_again(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items
        stand_in._live_fill_in_progress = True
        with patch.object(eng_module.threading, "Thread") as mock_thread:
            result = stand_in._try_extend_live_log_async()
        self.assertFalse(result)
        mock_thread.assert_not_called()

    def test_throttled_within_five_seconds(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items
        stand_in._last_live_extend_attempt = time.time()  # just attempted
        with patch.object(eng_module.threading, "Thread") as mock_thread:
            result = stand_in._try_extend_live_log_async()
        self.assertFalse(result)
        mock_thread.assert_not_called()

    def test_near_hour_boundary_does_not_dispatch_and_clears_guard(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items
        # 59:45 -- well under DURATION_FIT_MARGIN seconds left.
        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 6, 59, 45))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(eng_module.threading, "Thread") as mock_thread:
            result = stand_in._try_extend_live_log_async()
        self.assertFalse(result)
        mock_thread.assert_not_called()
        self.assertFalse(stand_in._live_fill_in_progress)

    def test_successful_dispatch_spawns_worker_with_captured_context_and_generation(self):
        stand_in = make_stand_in()
        stand_in._live_fill_generation = 0  # testing the dispatch counter itself, starts from scratch
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items

        captured = {}

        def fake_worker(*args, **kwargs):
            captured["args"] = args

        # Pinned wall clock to early-in-hour so the false-deficit
        # suppression check (added by the 2026-08-12 fix) sees a
        # large positive deficit (short 180s fixture item at :05:00
        # = ~3120s deficit, well above DURATION_FIT_MARGIN) and
        # dispatches -- this test's own contract is "dispatch DID
        # happen", separate from the suppression check tests below.
        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 6, 5, 0))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(stand_in, "_live_fill_worker", side_effect=fake_worker):
            result = stand_in._try_extend_live_log_async()

        deadline = time.time() + 5
        while "args" not in captured and time.time() < deadline:
            time.sleep(0.02)

        self.assertFalse(result)
        self.assertIn("args", captured)
        (log_id, generation, existing_picks, original_count, accumulated,
         hour_start, start_position, target_duration_seconds) = captured["args"]
        self.assertEqual(log_id, log.id)
        self.assertEqual(generation, 1)
        self.assertEqual(stand_in._live_fill_generation, 1)
        self.assertEqual(original_count, 1)
        self.assertEqual(start_position, 1)
        # New contract (2026-08-12 fix): target_duration_seconds is the
        # authoritative deficit computed from real engine state, not
        # NOMINAL_HOUR_SECONDS.
        self.assertGreater(target_duration_seconds, 0)
        self.assertLess(target_duration_seconds, eng_module.NOMINAL_HOUR_SECONDS,
                        "target_duration_seconds must be the DEFICIT, not a full hour")
        self.assertEqual(accumulated, 0.0,
                         "accumulated is now always 0 -- the deficit lives in target_duration_seconds")

    def test_each_dispatch_bumps_generation(self):
        stand_in = make_stand_in()
        stand_in._live_fill_generation = 0  # testing the dispatch counter itself, starts from scratch
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items

        # See test_successful_dispatch_... above for why the wall-clock
        # pin is needed for a dispatch-happens assertion.
        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 6, 5, 0))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(stand_in, "_live_fill_worker"):
            stand_in._try_extend_live_log_async()
        self.assertEqual(stand_in._live_fill_generation, 1)

        stand_in._live_fill_in_progress = False
        stand_in._last_live_extend_attempt = 0.0  # bypass throttle for the test
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(stand_in, "_live_fill_worker"):
            stand_in._try_extend_live_log_async()
        self.assertEqual(stand_in._live_fill_generation, 2)


class LiveFillWorkerIsReadOnlyTests(LiveFillFixtureMixin, TransactionTestCase):
    """The worker must NEVER write to PlaylistLog/LogItem itself --
    only compute a proposal and hand it to GLib.idle_add."""

    def test_worker_never_calls_append_fill_items(self):
        stand_in = make_stand_in()
        stand_in._live_fill_in_progress = True
        log, items = self.make_log_with_one_item(6)
        existing_picks = [{"track": items[0].track, "category": items[0].category}]
        hour_start = timezone.localtime().replace(minute=0, second=0, microsecond=0)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        with patch.object(eng_module, "GLib") as mock_glib, \
             patch.object(eng_module, "append_fill_items") as mock_append:
            stand_in._live_fill_worker(log.id, 1, existing_picks, 1, 0, hour_start, 1)

        mock_append.assert_not_called()
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertFalse(stand_in._live_fill_in_progress)
        # A real proposal WAS computed and handed off -- read-only, not inert.
        mock_glib.idle_add.assert_called_once()
        call_args = mock_glib.idle_add.call_args[0]
        self.assertEqual(call_args[0], stand_in._install_live_fill)
        self.assertEqual(call_args[1], log.id)
        self.assertEqual(call_args[2], 1)  # generation

    def test_no_glib_scheduled_when_not_running_and_no_db_write(self):
        stand_in = make_stand_in(running=False)
        stand_in._live_fill_in_progress = True
        log, items = self.make_log_with_one_item(6)
        existing_picks = [{"track": items[0].track, "category": items[0].category}]
        hour_start = timezone.localtime().replace(minute=0, second=0, microsecond=0)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        with patch.object(eng_module, "GLib") as mock_glib:
            stand_in._live_fill_worker(log.id, 1, existing_picks, 1, 0, hour_start, 1)

        mock_glib.idle_add.assert_not_called()
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertFalse(stand_in._live_fill_in_progress)

    def test_worker_exception_is_caught_no_db_write_guard_still_clears(self):
        stand_in = make_stand_in()
        stand_in._live_fill_in_progress = True
        log, items = self.make_log_with_one_item(6)
        existing_picks = [{"track": items[0].track, "category": items[0].category}]
        hour_start = timezone.localtime().replace(minute=0, second=0, microsecond=0)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        with patch.object(eng_module, "fill_remaining_hour", side_effect=RuntimeError("boom")):
            stand_in._live_fill_worker(log.id, 1, existing_picks, 1, 0, hour_start, 1)

        self.assertFalse(stand_in._live_fill_in_progress)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertTrue(SystemEvent.objects.filter(category="engine", title="Live-fill worker crashed").exists())


class InstallLiveFillStaleProposalTests(LiveFillFixtureMixin, TransactionTestCase):
    """Every scenario here must result in ZERO database mutation --
    asserted directly against LogItem.objects, not just self.log_items."""

    def test_shutdown_before_callback_zero_db_mutation(self):
        stand_in = make_stand_in(running=False)
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        picks = self.make_proposal_picks(1)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertEqual(len(stand_in.log_items), 1)

    def test_rollover_to_next_hour_zero_db_mutation(self):
        """Worker finishes after the engine has already rolled over to
        the next hour's log -- current_log.id no longer matches."""
        stand_in = make_stand_in()
        log_a, items_a = self.make_log_with_one_item(6)
        log_b, items_b = self.make_log_with_one_item(7)
        stand_in.current_log = log_b  # rolled over while worker ran
        stand_in.log_items = list(items_b)
        stand_in._live_fill_generation = 1  # matches the dispatch generation -- isolates THIS test's own check
        picks = self.make_proposal_picks(1)
        before_count_a = LogItem.objects.filter(playlist_log=log_a).count()

        result = stand_in._install_live_fill(log_a.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log_a).count(), before_count_a)
        self.assertEqual(stand_in.log_items, list(items_b))

    def test_active_log_replaced_by_different_build_zero_db_mutation(self):
        """Same (date, hour) is technically still 'current' in wall-clock
        terms, but self.current_log has been swapped to a DIFFERENT
        PlaylistLog row (e.g. an admin rebuild) -- id mismatch must still
        catch this."""
        stand_in = make_stand_in()
        log_original, items_original = self.make_log_with_one_item(6)
        log_replacement, items_replacement = self.make_log_with_one_item(6)  # deletes+recreates for hour 6
        stand_in.current_log = log_replacement
        stand_in.log_items = list(items_replacement)
        stand_in._live_fill_generation = 1  # matches the dispatch generation -- isolates THIS test's own check
        picks = self.make_proposal_picks(1)

        result = stand_in._install_live_fill(log_original.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log_replacement).count(), 1)

    def test_superseded_by_newer_generation_zero_db_mutation(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        stand_in._live_fill_generation = 2  # a newer dispatch has already happened
        picks = self.make_proposal_picks(1)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)  # stale generation=1

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertEqual(len(stand_in.log_items), 1)

    def test_another_extension_already_satisfied_the_gap_zero_db_mutation(self):
        """self.log_items grew since dispatch (e.g. a different install
        already ran) -- expected_prior_count mismatch must block a
        second, now-redundant append rather than risk duplicate
        coverage."""
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in._live_fill_generation = 1  # matches the dispatch generation -- isolates THIS test's own check
        # Simulate another extension having already landed one item.
        already_added = LogItem.objects.create(playlist_log=log, position=1, scheduled_time=timezone.now())
        stand_in.log_items = list(items) + [already_added]
        picks = self.make_proposal_picks(1)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        # expected_prior_count=1 (captured BEFORE the other extension landed)
        result = stand_in._install_live_fill(log.id, 1, picks, 1, 2)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertEqual(len(stand_in.log_items), 2)  # unchanged by this stale proposal

    def test_wall_clock_boundary_reached_since_dispatch_zero_db_mutation(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        picks = self.make_proposal_picks(1)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 6, 59, 50))  # 10s left, under margin
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now):
            result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertEqual(len(stand_in.log_items), 1)

    def test_missing_playlistlog_zero_db_mutation(self):
        stand_in = make_stand_in()
        nonexistent_id = 999999999
        stand_in.current_log = type("FakeLog", (), {"id": nonexistent_id})()
        stand_in.log_items = []
        picks = self.make_proposal_picks(1)

        result = stand_in._install_live_fill(nonexistent_id, 1, picks, 0, 0)

        self.assertFalse(result)
        self.assertEqual(len(stand_in.log_items), 0)


class InstallLiveFillValidProposalTests(LiveFillFixtureMixin, TransactionTestCase):
    def test_valid_proposal_db_append_then_memory_mutation(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        stand_in.decks = {"A": object(), "B": None}  # not idle -- no auto-start expected
        picks = self.make_proposal_picks(2)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        self.assertFalse(result)  # one-shot idle callback
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count + 2)
        self.assertEqual(len(stand_in.log_items), 3)
        stand_in._start_next_track.assert_not_called()
        self.assertTrue(SystemEvent.objects.filter(category="engine", title="Live log extension activated").exists())

    def test_idle_decks_trigger_start_next_track_after_successful_db_append(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        picks = self.make_proposal_picks(1)

        stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        stand_in._start_next_track.assert_called_once()

    def test_manual_mode_suppresses_auto_start_even_when_idle(self):
        stand_in = make_stand_in()
        stand_in.manual_mode = True
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        picks = self.make_proposal_picks(1)

        stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        stand_in._start_next_track.assert_not_called()

    def test_db_append_failure_leaves_memory_unchanged(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = list(items)
        picks = self.make_proposal_picks(1)
        before_count = LogItem.objects.filter(playlist_log=log).count()

        with patch.object(eng_module, "append_fill_items", side_effect=RuntimeError("db down")):
            result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)
        self.assertEqual(len(stand_in.log_items), 1, "memory must not be extended when the DB append failed")
        self.assertTrue(SystemEvent.objects.filter(category="engine", title="Live-fill DB append failed").exists())


class RollOverDispatchesAsyncTests(LiveFillFixtureMixin, TransactionTestCase):
    def test_no_next_hour_peek_dispatches_async_and_returns_false(self):
        stand_in = make_stand_in()
        log, items = self.make_log_with_one_item(6)
        stand_in.current_log = log
        stand_in.log_items = items

        with patch.object(stand_in, "_peek_next_hour", return_value=None), \
             patch.object(stand_in, "_try_extend_live_log_async", return_value=False) as mock_async:
            result = stand_in._roll_over_to_next_hour()

        self.assertFalse(result)
        mock_async.assert_called_once()
