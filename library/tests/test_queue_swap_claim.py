"""Queue Swap remediation, Codex F1: the engine's playback claim and replacements.

The claim adopts the COMMITTED track atomically under its row lock (however many swaps committed before
it), and a claim that cannot take the row lock in time is CONTENDED -- the same occurrence is put back as
the next item and retried (bounded, visibly) instead of being skipped. These drive the real
_start_next_track / _next_queue_item / _claim_playback_occurrence / retry callback against the real database;
only GStreamer deck construction is replaced by a stand-in that keeps _create_deck's claim contract (the
contract itself is asserted on the real _create_deck source)."""
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng
from library.models import LogItem
from library.tests.test_queue_swap import SwapFixtures
from monitoring.models import SystemEvent
from webrequests.models import SongRequest
from webrequests.services import mark_song_requests_aired

TIMEOUTS = []


class ClaimFixture(SwapFixtures):
    def setUp(self):
        self.build()
        # A plain music queue: [0] on air (deck A), [1] the swap target, [2] what follows.
        LogItem.objects.filter(pk=self.items[0].pk).update(playback_claimed_at=timezone.now(), played_at=timezone.now())
        self.engine = self.make_engine()
        self.opened = []
        TIMEOUTS.clear()
        patcher = patch.object(eng.GLib, "timeout_add", side_effect=lambda ms, fn, *args: TIMEOUTS.append((ms, fn, args)) or 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("maybe_schedule_song_request",):
            p = patch.object(eng, name, side_effect=lambda item, **kw: item)
            p.start()
            self.addCleanup(p.stop)

    def make_engine(self):
        engine = object.__new__(eng.PlaybackEngine)
        engine.running = True
        engine.manual_mode = False
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
        engine._next_triggered = True

        def create_deck(slot, item, *args, **kwargs):
            # Same claim contract as the real _create_deck (asserted below).
            claim = engine._claim_playback_occurrence(item)
            if claim is not True:
                if claim is eng.CLAIM_CONTENDED:
                    engine._claim_contended_item_id = item.id
                return None
            self.opened.append((slot, item.id, item.track_id, item.track.title))
            deck = SimpleNamespace(log_item=item)
            engine.decks[slot] = deck
            return deck
        engine._create_deck = create_deck
        return engine

    def start(self):
        self.engine._next_triggered = True
        self.engine._start_next_track(slot="B")

    def committed(self, item):
        return LogItem.objects.get(pk=item.pk)


class ClaimAdoptionTests(ClaimFixture, TransactionTestCase):
    def test_real_create_deck_keeps_the_claim_contract(self):
        source = inspect.getsource(eng.PlaybackEngine._create_deck)
        self.assertIn("claim = self._claim_playback_occurrence(log_item)", source)
        self.assertIn("if claim is CLAIM_CONTENDED:\n                self._claim_contended_item_id = log_item.id", source)
        claim_at = source.index("claim = self._claim_playback_occurrence(log_item)")
        # Everything after the claim opens what the claimed row commits to.
        self.assertIn("track = log_item.track\n        filepath = track.filepath", source[claim_at:])
        self.assertLess(claim_at, source.index('src.set_property("location", filepath)'))

    def test_a_to_b_committed_before_the_claim_plays_b(self):
        self.assertTrue(self.replace().changed)                    # memory still holds A
        self.start()
        self.assertEqual(self.opened, [("B", self.target.id, self.short.id, "Short Song")])
        row = self.committed(self.target)
        self.assertIsNotNone(row.playback_claimed_at)
        self.assertEqual(row.track_id, self.short.id)
        self.assertEqual(self.engine.log_items[1].track_id, self.short.id)   # the queue's own object adopted it
        self.assertEqual(self.engine._queue_cursor, 2)

    def test_claim_first_plays_a_and_refuses_the_later_swap(self):
        self.start()
        self.assertEqual(self.opened, [("B", self.target.id, self.long.id, "Long Song")])
        self.assertEqual(self.replace().reason, "claimed")
        self.assertEqual(self.committed(self.target).track_id, self.long.id)

    def test_a_to_b_to_c_before_the_claim_plays_c_in_the_original_occurrence(self):
        self.assertTrue(self.replace().changed)                                        # A -> B
        self.assertTrue(self.replace(expected=self.short.id, track=self.other).changed)  # B -> C
        self.start()
        self.assertEqual(self.opened, [("B", self.target.id, self.other.id, "Other Song")])
        self.assertEqual(self.engine._queue_cursor, 2)
        self.assertFalse(SystemEvent.objects.filter(title__icontains="claim").exists())

    def test_three_replacements_never_exhaust_anything_or_skip_the_row(self):
        for expected, track in ((self.long, self.short), (self.short, self.other), (self.other, self.short)):
            self.assertTrue(self.replace(expected=expected.id, track=track).changed)
        self.start()
        self.assertEqual([o[1:3] for o in self.opened], [(self.target.id, self.short.id)])

    def test_an_unplayable_committed_replacement_keeps_the_existing_skip_policy(self):
        self.assertTrue(self.replace().changed)
        Path(self.short.filepath).unlink()                         # the committed replacement lost its file
        self.start()
        # The occurrence is genuinely unplayable: the existing failure path opens what follows.
        self.assertEqual([o[1] for o in self.opened], [self.items[2].id])
        self.assertIsNone(self.committed(self.target).playback_claimed_at)
        self.assertEqual(self.engine._deck_open_failures, 1)
        self.assertFalse(TIMEOUTS)

    def test_a_web_request_swap_racing_the_claim_is_adopted_and_fulfilled_truthfully(self):
        request = SongRequest.objects.create(
            external_request_id="claim-race", track=self.other, status="scheduled", submitted_at=timezone.now(),
            log_item=self.target, scheduled_at=timezone.now(),
        )
        LogItem.objects.filter(pk=self.target.pk).update(track=self.other, track_title="Other Song")   # committed
        self.start()
        self.assertEqual([o[2] for o in self.opened], [self.other.id])
        deck_item = self.engine.decks["B"].log_item
        mark_song_requests_aired(deck_item, timezone.now())
        request.refresh_from_db()
        self.assertEqual(request.status, "fulfilled")


class _RowLock:
    """Hold the target LogItem's row lock from another connection (an in-flight Swap transaction)."""

    def __init__(self, item_id, *, then_track=None, seconds=1.2):
        self.item_id, self.then_track, self.seconds = item_id, then_track, seconds
        self.ready, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run)

    def run(self):
        try:
            with transaction.atomic():
                LogItem.objects.select_for_update().get(pk=self.item_id)
                if self.then_track is not None:
                    LogItem.objects.filter(pk=self.item_id).update(track=self.then_track)
                self.ready.set()
                self.release.wait(self.seconds)
        finally:
            connections.close_all()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(10)


class ClaimContentionTests(ClaimFixture, TransactionTestCase):
    def test_a_lock_held_past_the_claim_timeout_never_skips_the_occurrence(self):
        with _RowLock(self.target.id, seconds=10):
            self.start()
            self.assertEqual(self.opened, [])                                   # nothing else was opened
            self.assertEqual(self.engine._queue_cursor, 1)                      # the target is next again
            self.assertEqual(self.engine._next_queue_item_id(), self.target.id)
            self.assertEqual(self.engine._claim_contention, {"item_id": self.target.id, "attempts": 1})
            self.assertFalse(self.engine._next_triggered)                       # the trigger may retry
            self.assertEqual(len(TIMEOUTS), 1)
            self.assertEqual(TIMEOUTS[0][0], eng.CLAIM_CONTENTION_RETRY_MS)
            self.assertIsNone(self.committed(self.target).playback_claimed_at)
            self.assertIsNone(self.committed(self.items[2]).playback_claimed_at)

    def test_contention_that_resolves_opens_the_original_occurrence_with_its_committed_track(self):
        with _RowLock(self.target.id, then_track=self.short, seconds=10):      # a Swap still committing
            self.start()
        _ms, callback, args = TIMEOUTS[-1]
        callback(*args)                                                          # the armed retry
        self.assertEqual(self.opened, [("B", self.target.id, self.short.id, "Short Song")])
        self.assertIsNone(self.engine._claim_contention)
        self.assertEqual(self.engine._queue_cursor, 2)
        callback(*args)                                                          # a stale retry is inert
        self.assertEqual(len(self.opened), 1)

    def test_repeated_contention_is_bounded_and_visible_and_never_claims_twice(self):
        with _RowLock(self.target.id, seconds=20):
            self.start()
            for _ in range(eng.CLAIM_CONTENTION_MAX_ATTEMPTS - 1):
                _ms, callback, args = TIMEOUTS[-1]
                callback(*args)
            self.assertEqual(self.engine._claim_contention["attempts"], eng.CLAIM_CONTENTION_MAX_ATTEMPTS)
            self.assertEqual(self.opened, [])
            _ms, callback, args = TIMEOUTS[-1]
            callback(*args)                                                       # one more: over the bound
        self.assertTrue(SystemEvent.objects.filter(title="Occurrence skipped after persistent claim contention",
                                                   level="error").exists())
        self.assertEqual([o[1] for o in self.opened], [self.items[2].id])        # the visible skip
        self.assertIsNone(self.committed(self.target).playback_claimed_at)
        self.assertEqual(LogItem.objects.filter(playlist_log=self.log, playback_claimed_at__isnull=False)
                         .exclude(pk=self.items[0].pk).count(), 1)

    def test_pending_retries_are_inert_after_shutdown_manual_rollover_or_a_newer_attempt(self):
        with _RowLock(self.target.id, seconds=10):
            self.start()
        _ms, callback, args = TIMEOUTS[-1]
        for change, restore in (
            (lambda: setattr(self.engine, "running", False), lambda: setattr(self.engine, "running", True)),
            (lambda: setattr(self.engine, "manual_mode", True), lambda: setattr(self.engine, "manual_mode", False)),
            (lambda: self.engine.__dict__.update(_claim_contention={"item_id": self.target.id, "attempts": 5}),
             lambda: self.engine.__dict__.update(_claim_contention={"item_id": self.target.id, "attempts": 1})),
        ):
            change()
            callback(*args)
            self.assertEqual(self.opened, [])
            restore()
        # Rollover: a new hour's queue replaced the old one -- the occurrence is no longer next.
        saved = (self.engine.log_items, self.engine._queue_cursor)
        self.engine.log_items, self.engine._queue_cursor = [self.items[6]], 0
        callback(*args)
        self.assertEqual(self.opened, [])
        self.engine.log_items, self.engine._queue_cursor = saved
        # Manual mode leaves it next: releasing Manual starts it through the ordinary path.
        callback(*args)
        self.assertEqual([o[1] for o in self.opened], [self.target.id])

    def test_a_forced_occurrence_returns_to_the_front_of_the_forced_list(self):
        forced = self.engine.log_items[2]
        self.engine._forced_next_items = [forced]
        with _RowLock(forced.id, seconds=10):
            self.start()
        self.assertEqual([i.id for i in self.engine._forced_next_items], [forced.id])
        self.assertEqual(self.engine._queue_cursor, 1)
        _ms, callback, args = TIMEOUTS[-1]
        callback(*args)
        self.assertEqual([o[1] for o in self.opened], [forced.id])

    def test_an_already_claimed_occurrence_keeps_the_existing_failure_handling(self):
        LogItem.objects.filter(pk=self.target.pk).update(playback_claimed_at=timezone.now())
        self.engine.log_items[1].playback_claimed_at = timezone.now()
        self.assertFalse(self.engine._defer_contended_occurrence(self.engine.log_items[1], False, None))
