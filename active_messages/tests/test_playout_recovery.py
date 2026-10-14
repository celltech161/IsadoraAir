"""D2B recovery acts only on POSITIVE evidence that the current attempt can no
longer air -- never on elapsed time alone:

* an ACCEPTED item may wait in the queue indefinitely (long-form, Manual,
  mic, Remote DJ) without consuming attempts;
* a CLAIMED attempt is never superseded by age (attention only), and is
  re-sent after a failed deck open only when no deck was ever built;
* a native item the engine discards before a deck, or that a queue
  replacement drops, is reported at once: re-sent if current, SUPPRESSED if
  stale, FAILED (publication_unavailable) if its Track is gone;
* an engine restart fences and recovers the previous process's attempt.

TransactionTestCase (with the seeded catalog restored) because the engine's
own code recycles its database connection."""
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from active_messages import playout, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastMessageEvent
from library.models import LogItem, Track
from production.tests.support import ffmpeg_available

from .d2b_support import Playout, engine_stand_in

INSTANCE = eng_module.ENGINE_INSTANCE
LONG_AFTER = timedelta(hours=3)          # far beyond the former 600-second re-send threshold


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RecoveryTests(Playout, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        # Song A is on air: the cursor already stepped past it.
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)
        self.execution, self.publication, command = self.dispatched()
        self.engine._dispatch_engine_command(command)
        self.native = LogItem.objects.get(category__code="UrgentPA")
        self.attempt = self.stored().attempt_token

    def stored(self):
        return ActiveBroadcastExecution.objects.get(pk=self.execution.pk)

    def attention(self):
        patcher = mock.patch("monitoring.models.emit_event")
        emitted = patcher.start()
        self.addCleanup(patcher.stop)
        return emitted

    def attention_codes(self, emitted):
        return [call.kwargs.get("detail", {}).get("code") for call in emitted.call_args_list]

    # -- ACCEPTED waits; time alone never re-sends ------------------------------------------
    def test_an_accepted_item_can_wait_in_the_queue_far_beyond_the_old_threshold(self):
        later = timezone.now() + LONG_AFTER
        with self.committing():
            for step in range(5):
                self.assertEqual(playout.advance(now=later + timedelta(minutes=step * 20)), "accepted")
            self.engine._sweep_active_message_queue()             # still reachable in the queue
        stored = self.stored()
        self.assertEqual((stored.state, stored.attempt_token, stored.attempt_count), ("accepted", self.attempt, 1))
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(playout.inspect(self.native, INSTANCE).allowed)
        self.assertEqual(self.engine._next_queue_item()[0].id, self.native.id)   # and it is still next

    def test_a_manual_hold_or_long_program_consumes_no_attempts(self):
        self.engine.manual_mode = True                             # the queue is held: nothing advances
        with self.committing():
            for hours in (1, 2, 6, 12):
                self.assertEqual(playout.advance(now=timezone.now() + timedelta(hours=hours)), "accepted")
                self.engine._sweep_active_message_queue()
        self.assertEqual((self.stored().attempt_count, len(self.commands)), (1, 1))
        self.assertEqual(ActiveBroadcastInsertion.objects.count(), 1)
        self.engine.manual_mode = False
        self.assertEqual(self.engine._next_queue_item()[0].id, self.native.id)

    # -- CLAIMED is never superseded by age ------------------------------------------------
    def test_a_claimed_attempt_is_never_superseded_by_age(self):
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)
        emitted = self.attention()
        with self.committing():
            for hours in (1, 3, 24):
                self.assertEqual(playout.advance(now=timezone.now() + timedelta(hours=hours)), "claimed")
        stored = self.stored()
        self.assertEqual((stored.state, stored.attempt_token, stored.attempt_count), ("claimed", self.attempt, 1))
        self.assertEqual(len(self.commands), 1)
        self.assertIn("claimed_not_started", self.attention_codes(emitted))   # attention only
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)   # its deck stays authorized

    def test_a_claimed_attempt_whose_deck_may_have_existed_is_held_not_re_sent(self):
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)
        LogItem.objects.filter(pk=self.native.pk).update(playback_claimed_at=timezone.now())   # deck construction began
        emitted = self.attention()
        with self.committing():
            self.assertEqual(playout.report_not_aired(self.native.pk, INSTANCE, stage="deck_not_opened"), "held")
        self.assertEqual((self.stored().state, self.stored().attempt_token), ("claimed", self.attempt))
        self.assertEqual(len(self.commands), 1)
        self.assertIn("claimed_not_aired", self.attention_codes(emitted))

    def test_a_claimed_attempt_reported_by_another_process_is_held(self):
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)
        with self.committing():
            self.assertEqual(playout.report_not_aired(self.native.pk, "another-engine", stage="discarded"), "held")
        self.assertEqual(len(self.commands), 1)

    def test_a_claimed_attempt_that_provably_never_built_a_deck_is_re_sent(self):
        # Deck-open authority claimed it, then the playback claim (which
        # precedes every deck construction) failed: _create_deck returned None.
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)
        with self.committing():
            self.engine._report_active_message_left(self.native.pk, "deck_not_opened")
        self.assertEqual(len(self.commands), 2)
        self.assertNotEqual(self.stored().attempt_token, self.attempt)
        self.assertFalse(playout.authorize_open(self.native, INSTANCE).allowed)   # the old one is dead
        self.engine._dispatch_engine_command(self.commands[-1])
        self.assertEqual(self.stored().state, "accepted")

    def test_a_re_sent_attempt_whose_command_is_lost_is_re_sent_after_the_delivery_timeout(self):
        with self.committing():
            playout.report_not_aired(self.native.pk, INSTANCE, stage="deck_not_opened")
            self.assertEqual(playout.advance(), "in_flight")        # within the delivery bound
            later = timezone.now() + timedelta(seconds=playout.DISPATCH_TIMEOUT_SECONDS + 1)
            self.assertEqual(playout.advance(now=later), "resent")  # never accepted: positively lost
        self.assertEqual(len(self.commands), 3)
        self.engine._dispatch_engine_command(self.commands[1])      # the lost one arrives late: harmless
        self.assertEqual(ActiveBroadcastInsertion.objects.count(), 1)
        self.engine._dispatch_engine_command(self.commands[2])
        self.assertEqual((self.stored().state, ActiveBroadcastInsertion.objects.count()), ("accepted", 2))

    # -- positive discard evidence ---------------------------------------------------------
    def test_a_current_native_item_skipped_before_deck_open_is_re_sent_at_once(self):
        # A transient library stat failure makes the engine's playability gate
        # discard the current item; by the report the file is back.
        real_is_file = Path.is_file
        native_path = Path(self.native.track.filepath)
        failures = {"left": 1}

        def flaky(path):
            if path == native_path and failures["left"]:
                failures["left"] -= 1
                return False
            return real_is_file(path)

        with mock.patch.object(Path, "is_file", flaky):
            with self.committing():
                item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.song_b.id)                   # B plays next, not lost or repeated
        self.assertEqual(len(self.commands), 2)                     # re-sent at once, no timer
        resent = self.commands[-1]
        self.assertNotEqual(resent["attempt"], str(self.attempt))
        self.assertIn("not_aired", list(ActiveBroadcastMessageEvent.objects.filter(execution=self.execution)
                                        .values_list("event", flat=True)))
        self.assertFalse(self.engine._active_message_held)
        self.engine._dispatch_engine_command(resent)               # the same aggregate goes back in the queue
        self.assertEqual(self.stored().state, "accepted")
        reinserted = self.engine.log_items[self.engine._queue_cursor]
        self.assertEqual(reinserted.track_id, self.publication.track_id)
        self.assertEqual(self.engine._next_queue_item()[0].id, reinserted.id)

    def test_a_stale_native_item_skipped_is_suppressed_and_not_re_sent(self):
        services.set_limits(actor=self.admin, max_active_messages=10)    # the composition moved on
        item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.song_b.id)
        stored = self.stored()
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))
        self.assertEqual(len(self.commands), 1)

    def test_an_ordinary_unplayable_item_is_skipped_exactly_as_before(self):
        missing = self.song("Gone")
        gone = LogItem.objects.create(playlist_log=self.log, position=40, scheduled_time=timezone.now(),
                                      track=missing, track_title=missing.title, category=missing.category)
        Path(missing.filepath).unlink()
        self.engine.log_items.insert(1, gone)
        with mock.patch("active_messages.playout.report_not_aired",
                        side_effect=AssertionError("no active-message work for ordinary tracks")):
            item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.native.id)                  # the ordinary item skipped, nothing else
        self.assertEqual(len(self.commands), 1)

    def test_an_ordinary_track_less_ghost_is_only_looked_up_and_left_alone(self):
        ordinary = self.song("Deleted song")
        ghost = LogItem.objects.create(playlist_log=self.log, position=40, scheduled_time=timezone.now(),
                                       track=ordinary, track_title=ordinary.title, category=ordinary.category)
        ordinary.delete()
        ghost.refresh_from_db()
        self.engine.log_items.insert(1, ghost)
        with mock.patch("active_messages.services._lock_policy", side_effect=AssertionError("no lock")):
            item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.native.id)
        self.assertEqual((self.stored().state, len(self.commands)), ("accepted", 1))
        self.assertFalse(self.engine._active_message_held.get(ghost.id))

    def test_a_track_deleted_after_insertion_fails_closed_at_once_without_a_crash(self):
        Track.objects.filter(pk=self.publication.track_id).delete()
        self.engine.log_items = list(self.log.items.select_related("track", "category").order_by("position"))
        self.assertIsNone(self.engine.log_items[1].track_id)       # the engine's reload sees a ghost
        emitted = self.attention()
        with self.committing():
            item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.song_b.id)
        stored = self.stored()
        self.assertEqual((stored.state, stored.failure_code), ("failed", "publication_unavailable"))
        self.assertEqual(len(self.commands), 1)                     # nothing that could only be refused
        self.assertIn("publication_unavailable", self.attention_codes(emitted))

    def test_a_track_less_ghost_from_another_process_is_correlated_by_its_snapshot(self):
        Track.objects.filter(pk=self.publication.track_id).delete()
        self.engine._active_message_held.clear()                    # not this process's insertion
        self.engine.log_items = list(self.log.items.select_related("track", "category").order_by("position"))
        with self.committing():
            self.engine._next_queue_item()
        self.assertEqual(self.stored().failure_code, "publication_unavailable")

    def test_a_queue_replacement_that_drops_the_item_is_detected_and_re_sent(self):
        # e.g. the next hour's log installed ahead of the top of the hour.
        next_log, next_items = self.playlist("Next hour 1", "Next hour 2", hour=11)
        first_of_next_hour = next_items[0].id
        self.engine.current_log, self.engine.log_items, self.engine._queue_cursor = next_log, list(next_items), 0
        with self.committing():
            self.engine._sweep_active_message_queue()
        self.assertEqual(len(self.commands), 2)
        self.assertFalse(playout.inspect(self.native, INSTANCE).allowed)   # the dropped item is dead
        self.engine._dispatch_engine_command(self.commands[-1])
        self.assertEqual(self.engine._next_queue_item()[0].track_id, self.publication.track_id)
        self.assertEqual(self.engine._next_queue_item()[0].id, first_of_next_hour)

    def test_a_deleted_log_item_is_positive_evidence_for_the_worker(self):
        LogItem.objects.filter(pk=self.native.pk).delete()          # e.g. a play-now replaced the hour
        with self.committing():
            self.assertEqual(playout.advance(), "resent")
        self.assertEqual(len(self.commands), 2)
        # ...and the engine's in-memory copy can never open a deck.
        self.assertFalse(playout.authorize_open(self.native, INSTANCE).allowed)

    def test_deck_open_never_claims_a_deleted_log_item(self):
        LogItem.objects.filter(pk=self.native.pk).delete()
        decision = playout.authorize_open(self.native, INSTANCE)
        self.assertFalse(decision.allowed)
        self.assertEqual(self.stored().state, "accepted")           # not claimed: the report can re-send it

    def test_the_engine_reports_a_deck_that_did_not_open(self):
        self.engine._free_slot = lambda: "A"
        self.engine._deck_slot_available = lambda slot: True
        self.engine._on_log_exhausted = lambda slot: None
        opened = []

        def create_deck(slot, log_item):
            opened.append(log_item.id)
            return None if log_item.id == self.native.id else object()

        self.engine._create_deck = create_deck
        with self.committing():
            self.engine._start_next_track(slot="A")
        self.assertEqual(opened, [self.native.id, self.song_b.id])   # B follows at once
        self.assertEqual(len(self.commands), 2)                      # the native airing re-sent

    def test_a_failed_report_is_retried_by_the_queue_sweep(self):
        next_log, next_items = self.playlist("Next hour 1", hour=11)
        self.engine.current_log, self.engine.log_items, self.engine._queue_cursor = next_log, next_items, 0
        with mock.patch("active_messages.playout.report_not_aired", side_effect=RuntimeError("lock timeout")):
            self.engine._sweep_active_message_queue()
        self.assertIn(self.native.pk, self.engine._active_message_held)
        with mock.patch("active_messages.playout.report_not_aired", side_effect=AssertionError("throttled")):
            self.engine._sweep_active_message_queue()                # no tight retry loop on the GLib thread
        self.engine._active_message_report_retry_at = 0.0           # the retry interval has passed
        with self.committing():
            self.engine._sweep_active_message_queue()
        self.assertEqual(len(self.commands), 2)
        self.assertFalse(self.engine._active_message_held)

    # -- the restart fence ------------------------------------------------------------------
    def test_a_restart_fences_and_recovers_a_previous_process_claim(self):
        self.assertTrue(playout.authorize_open(self.native, "the-previous-process").allowed)
        with self.committing():
            playout.engine_started("the-next-process")
        self.assertEqual(len(self.commands), 2)
        self.assertNotEqual(self.stored().attempt_token, self.attempt)
        self.assertFalse(playout.authorize_open(self.native, "the-next-process").allowed)
        self.engine._dispatch_engine_command(self.commands[-1])
        self.assertEqual(self.stored().state, "accepted")
