"""Codex adversarial reproductions for Queue Swap (remediation of 9f230bf). Written against behavior only --
no name introduced by the remediation -- so the SAME file runs against the reviewed candidate as a negative
control (where all three fail) and against the corrected candidate (where all three pass):

1. a second valid Swap committing while the engine adopts the first never makes the engine skip the row;
2. a Swap holding the row lock past the claim's lock timeout never makes the engine skip the row;
3. a live-fill proposal whose shortage a later Swap removed never appends filler."""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng
from library.models import LogItem
from library.services import queue_replace
from library.tests.test_queue_swap import SwapFixtures
from library.tests import test_queue_swap_airtime as airtime


class AdversarialClaimTests(SwapFixtures, TransactionTestCase):
    def setUp(self):
        self.build()
        LogItem.objects.filter(pk=self.items[0].pk).update(playback_claimed_at=timezone.now())
        engine = object.__new__(eng.PlaybackEngine)
        engine.running, engine.manual_mode = True, False
        engine._lock = threading.RLock()
        engine._poison_skip_identities = set()
        engine._forced_next_items = []
        engine.current_log = self.log
        engine.log_items = list(self.log.items.select_related("track", "category", "category__kind").order_by("position"))
        engine._queue_cursor = 1
        engine.decks = {"A": SimpleNamespace(log_item=engine.log_items[0]), "B": None}
        engine._deck_slot_available = lambda slot: True
        engine._free_slot = lambda: next((s for s, d in engine.decks.items() if d is None), None)
        engine._on_log_exhausted = lambda slot: None
        engine._roll_over_to_next_hour = lambda: False
        engine._maybe_insert_dedication_intro = lambda item: item
        self.opened = []

        def create_deck(slot, item, *args, **kwargs):
            claim = engine._claim_playback_occurrence(item)
            contended = getattr(eng, "CLAIM_CONTENDED", None)
            if claim is not True:
                if contended is not None and claim is contended:
                    engine._claim_contended_item_id = item.id
                return None
            self.opened.append((item.id, item.track_id))
            engine.decks[slot] = SimpleNamespace(log_item=item)
            return engine.decks[slot]
        engine._create_deck = create_deck
        self.engine = engine
        for target, value in ((eng, "maybe_schedule_song_request"),):
            p = patch.object(target, value, side_effect=lambda item, **kw: item)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(eng.GLib, "timeout_add", return_value=1)
        p.start()
        self.addCleanup(p.stop)

    def start(self):
        self.engine._next_triggered = True
        self.engine._start_next_track(slot="B")

    def test_repeated_swap_during_adoption_never_skips_the_row(self):
        self.assertTrue(self.replace().changed)                        # A -> B; the engine still holds A
        real_playable = eng._log_item_playable
        fired = []

        def second_swap_meanwhile(item):
            # While the engine is adopting B, an operator commits B -> C from another connection.
            if not fired and item.id == self.target.id and item.track_id == self.short.id:
                fired.append(True)
                t = threading.Thread(target=self._swap_b_to_c)
                t.start()
                t.join(5)
            return real_playable(item)
        with patch.object(eng, "_log_item_playable", side_effect=second_swap_meanwhile):
            self.start()
        committed = LogItem.objects.get(pk=self.target.pk)
        # The row is never skipped, and what opened is exactly what the claimed row commits to.
        self.assertEqual([o[0] for o in self.opened], [self.target.id], self.opened)
        self.assertEqual(self.opened[0][1], committed.track_id)
        self.assertIsNotNone(committed.playback_claimed_at)

    def _swap_b_to_c(self):
        try:
            queue_replace.replace_upcoming_item(item_id=self.target.id, expected_track_id=self.short.id,
                                                track_id=self.other.id, engine_instance="engine-instance-1",
                                                state={**self.state, "timestamp": time.time()})
        finally:
            connections.close_all()

    def test_a_row_lock_held_past_the_claim_timeout_never_skips_the_row(self):
        ready, release = threading.Event(), threading.Event()

        def hold():
            try:
                with transaction.atomic():
                    LogItem.objects.select_for_update().get(pk=self.target.pk)
                    ready.set()
                    release.wait(10)
            finally:
                connections.close_all()
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            self.start()
        finally:
            release.set()
            worker.join(10)
        # Nothing after the target was opened or claimed while it was contended ...
        self.assertNotIn(self.items[2].id, [o[0] for o in self.opened])
        self.assertIsNone(LogItem.objects.get(pk=self.items[2].pk).playback_claimed_at)
        # ... and the target is still what plays next.
        self.start()
        self.assertEqual([o[0] for o in self.opened], [self.target.id])


class AdversarialLiveFillTests(TransactionTestCase):
    setUp = airtime.SwapAirtimeTests.setUp
    track = airtime.SwapAirtimeTests.track
    item = airtime.SwapAirtimeTests.item
    stand_in = airtime.SwapAirtimeTests.stand_in
    reload = airtime.SwapAirtimeTests.reload

    def swap(self, item, track):
        item = LogItem.objects.get(pk=item.pk)
        state = {"timestamp": time.time(), "state_revision": {"instance": "airtime-engine", "seq": 1},
                 "decks": {"A": {"log_item_id": self.items[0].id}}, "queue": [{"item_id": i.id} for i in self.items[1:]]}
        outcome = queue_replace.replace_upcoming_item(item_id=item.id, expected_track_id=item.track_id,
                                                      track_id=track.id, engine_instance="airtime-engine", state=state)
        self.assertTrue(outcome.changed, outcome.reason)

    def test_a_stale_proposal_cannot_append_after_the_shortage_disappears(self):
        filler = self.track("Filler", 180.0, 178.0)
        self.swap(self.items[1], self.short)               # a real shortage
        self.reload()
        thread = MagicMock()
        with patch.object(eng.timezone, "localtime", return_value=self.now), patch.object(eng.threading, "Thread", thread):
            self.engine._try_extend_live_log_async()
        log_id, generation, _existing, count, _acc, _hour, start_position, _target = thread.call_args.kwargs["args"]
        self.swap(self.items[1], self.long)                # the operator restores the longer track: no shortage
        self.engine._start_next_track = MagicMock()
        with patch.object(eng.timezone, "localtime", return_value=self.now):
            self.engine._install_live_fill(log_id, generation, [{"track": filler, "category": self.cat,
                                           "scheduled_time": self.now + timedelta(minutes=8)}], count, start_position)
        self.assertEqual(list(LogItem.objects.filter(playlist_log=self.log, origin=LogItem.ORIGIN_LIVE_FILL)), [])
        self.assertEqual(len(self.engine.log_items), 3)
