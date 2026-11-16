"""Queue Swap remediation, Codex F2: a normal live-fill proposal is bound to the queue it was computed
from. The dispatcher records the ordered (LogItem id, Track id) identity per generation; the installer
compares it with memory, then -- holding the PlaylistLog lock in the same transaction as the append --
with the committed queue. Every test asserts the database rows and the engine's queue, not return codes."""
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import connections, transaction
from django.test import TransactionTestCase

import library.services.engine as eng
from library.models import LogItem, PlaylistLog
from library.tests import test_queue_swap_airtime as airtime
from library.views import _reposition_items
from monitoring.models import SystemEvent


class LiveFillIdentityTests(TransactionTestCase):
    setUp = airtime.SwapAirtimeTests.setUp
    track = airtime.SwapAirtimeTests.track
    item = airtime.SwapAirtimeTests.item
    stand_in = airtime.SwapAirtimeTests.stand_in
    swap = airtime.SwapAirtimeTests.swap
    reload = airtime.SwapAirtimeTests.reload

    def dispatch(self, at=None):
        """Real dispatcher; the worker thread is replaced, its arguments returned."""
        self.engine._last_live_extend_attempt = 0.0
        thread = MagicMock()
        with patch.object(eng.timezone, "localtime", return_value=at or self.now), \
                patch.object(eng.threading, "Thread", thread):
            self.engine._try_extend_live_log_async()
        self.engine._live_fill_in_progress = False
        return thread.call_args.kwargs["args"] if thread.called else None

    def install(self, args, at=None, picks=None):
        """What the worker's GLib callback does with its proposal (picks: a filler of known airtime)."""
        log_id, generation, _existing, count, _acc, _hour_start, start_position, _target = args
        picks = picks or [{"track": self.filler, "category": self.cat, "scheduled_time": self.now + timedelta(minutes=8)}]
        self.engine._start_next_track = MagicMock()
        with patch.object(eng.timezone, "localtime", return_value=at or self.now):
            self.engine._install_live_fill(log_id, generation, picks, count, start_position)

    def fill_rows(self):
        return list(LogItem.objects.filter(playlist_log=self.log, origin=LogItem.ORIGIN_LIVE_FILL))

    def shortage(self):
        """A real shortage after a shortening Swap: returns the recorded proposal arguments."""
        self.filler = self.track("Filler", 180.0, 178.0)
        self.swap(self.items[1], self.short)
        self.reload()
        args = self.dispatch()
        self.assertIsNotNone(args)
        self.assertEqual(self.engine._live_fill_queue_identity,
                         (args[1], self.log.id, tuple((i.id, i.track_id) for i in self.engine.log_items)))
        return args

    def test_no_concurrent_change_installs_exactly_as_before(self):
        args = self.shortage()
        self.install(args)
        rows = self.fill_rows()
        self.assertEqual([(r.track_id, r.position) for r in rows], [(self.filler.id, 3)])
        self.assertEqual([i.id for i in self.engine.log_items[-1:]], [rows[0].id])
        self.assertEqual(len(self.engine.log_items), 4)

    def test_a_longer_swap_committed_after_dispatch_voids_the_now_unneeded_fill(self):
        args = self.shortage()
        self.swap(self.items[1], self.long)                # shortage gone; same length, memory not reloaded
        self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(self.engine.log_items), 3)

    def test_the_voided_proposal_is_reevaluated_from_the_new_queue(self):
        args = self.shortage()
        shorter = self.track("Shorter Still", 60.0, 50.0)
        self.swap(self.items[1], shorter)                  # an even bigger shortage after dispatch
        self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.reload()
        again = self.dispatch()
        self.assertGreater(again[7], args[7])              # the new deficit reflects the new track
        self.install(again)
        self.assertEqual(len(self.fill_rows()), 1)

    def test_a_reload_that_already_adopted_the_swap_voids_it_in_memory_without_querying(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        args = self.shortage()
        self.swap(self.items[1], self.long)
        self.reload()                                      # memory now holds the longer track
        with CaptureQueriesContext(connection) as queries:
            self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(queries), 0)

    def test_the_same_length_with_a_changed_order_voids_it(self):
        args = self.shortage()
        rows = list(self.log.items.order_by("position"))
        _reposition_items([rows[0], rows[2], rows[1]])
        self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(self.engine.log_items), 3)

    def test_fill_committing_first_leaves_a_later_swap_to_its_own_contract(self):
        args = self.shortage()
        self.install(args)
        self.assertEqual(len(self.fill_rows()), 1)
        self.swap(self.items[2], self.long)                # a later permitted operator action
        self.assertEqual(LogItem.objects.get(pk=self.items[2].pk).track_id, self.long.id)
        self.assertEqual(len(self.fill_rows()), 1)

    def test_a_swap_holding_the_log_lock_makes_the_install_discard_after_a_bounded_wait(self):
        args = self.shortage()
        ready, release = threading.Event(), threading.Event()

        def hold():
            try:
                with transaction.atomic():
                    PlaylistLog.objects.select_for_update().get(pk=self.log.pk)
                    ready.set()
                    release.wait(5)
            finally:
                connections.close_all()
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            started = eng.time.monotonic()
            self.install(args)
            waited = eng.time.monotonic() - started
        finally:
            release.set()
            worker.join(10)
        self.assertLess(waited, 2.0)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(self.engine.log_items), 3)
        self.assertFalse(SystemEvent.objects.filter(title="Live-fill DB append failed").exists())

    def test_a_failed_append_leaves_the_engine_queue_unchanged(self):
        args = self.shortage()
        with patch.object(eng, "append_fill_items", side_effect=RuntimeError("disk full")):
            self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(self.engine.log_items), 3)
        self.assertTrue(SystemEvent.objects.filter(title="Live-fill DB append failed").exists())

    def test_a_proposal_for_an_hour_no_longer_active_or_past_its_boundary_never_lands(self):
        args = self.shortage()
        late = self.now.replace(minute=59, second=45)
        self.install(args, at=late)
        self.assertEqual(self.fill_rows(), [])
        self.engine.current_log = PlaylistLog.objects.create(date=self.now.date(), hour=15, status="approved")
        self.install(args)
        self.assertEqual(self.fill_rows(), [])

    def test_a_causal_refill_that_owns_the_hour_still_blocks_the_install(self):
        args = self.shortage()
        self.engine._refill_coordinator = SimpleNamespace(
            blocks_legacy=lambda log_id, items: True, legacy_discards=0, clear_legacy_pending=lambda generation: None)
        self.install(args)
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(self.engine._refill_coordinator.legacy_discards, 1)

    def test_poison_guarded_rows_do_not_make_every_proposal_look_stale(self):
        self.filler = self.track("Filler", 180.0, 178.0)
        # Mid-queue (a trailing guarded row would collide with start_position, a pre-existing r0140 trait).
        LogItem.objects.filter(pk=self.items[2].pk).update(position=3)
        poisoned = self.item(2, self.after)
        self.engine._poison_skip_identities = {(self.log.id, poisoned.id)}
        self.swap(self.items[1], self.short)
        self.reload()                                      # the engine's queue omits the poisoned row
        self.assertNotIn(poisoned.id, [i.id for i in self.engine.log_items])
        args = self.dispatch()
        self.install(args)
        self.assertEqual(len(self.fill_rows()), 1)
