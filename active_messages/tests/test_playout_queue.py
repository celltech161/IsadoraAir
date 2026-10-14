"""D2B through the EXISTING urgent insertion and queue: the exact Track of one
execution attempt is spliced by the engine's own path, Weather/AMBER keep
priority, duplicates/delays/losses are harmless, and a stale aggregate is
skipped without consuming the next ordinary item.

TransactionTestCase (with the seeded catalog restored) because the engine's
own code recycles its database connection, as in the existing urgent-insert
tests."""
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from active_messages import playout, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastInsertion
from library.models import LogItem, Track
from production.tests.support import ffmpeg_available

from .d2b_support import Playout, engine_stand_in

INSTANCE = eng_module.ENGINE_INSTANCE


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class InsertionTests(Playout, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        # Song A is on air: the cursor already stepped past it.
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)

    def insert(self, payload):
        self.engine._dispatch_engine_command(payload)

    def queue_walk(self, count):
        return [self.engine._next_queue_item()[0] for _ in range(count)]

    def test_the_exact_track_is_spliced_by_the_existing_urgent_path(self):
        execution, publication, command = self.dispatched()
        self.assertEqual((command["command"], command["category"], command["track"]),
                         ("insert_urgent", "UrgentPA", publication.track_id))
        self.insert(command)
        native = self.engine.log_items[1]
        self.assertEqual((native.track_id, native.category.code), (publication.track_id, "UrgentPA"))
        self.assertEqual([item.id for item in self.engine.log_items], [self.song_a.id, native.id, self.song_b.id])
        insertion = ActiveBroadcastInsertion.objects.get()
        self.assertEqual((insertion.log_item_id, str(insertion.attempt_token), insertion.engine_instance),
                         (native.id, command["attempt"], INSTANCE))
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.engine_instance), ("accepted", INSTANCE))
        self.assertEqual([item.id for item in self.queue_walk(2)], [native.id, self.song_b.id])

    def test_a_duplicate_command_inserts_nothing_more(self):
        _execution, _publication, command = self.dispatched()
        self.insert(command)
        self.insert(dict(command))
        self.assertEqual(ActiveBroadcastInsertion.objects.count(), 1)
        self.assertEqual(LogItem.objects.filter(category__code="UrgentPA").count(), 1)
        self.assertEqual(len(self.engine.log_items), 3)

    def test_a_lost_command_is_re_sent_and_the_delayed_original_is_harmless(self):
        execution, _publication, lost = self.dispatched()
        later = timezone.now() + timedelta(seconds=playout.DISPATCH_TIMEOUT_SECONDS + 1)
        with self.committing():
            self.assertEqual(playout.advance(now=later), "dispatched")
        resent = self.commands[-1]
        self.assertNotEqual(resent["attempt"], lost["attempt"])
        self.insert(lost)                                         # the original finally arrives: superseded
        self.assertEqual(LogItem.objects.filter(category__code="UrgentPA").count(), 0)
        self.insert(resent)
        self.assertEqual(LogItem.objects.filter(category__code="UrgentPA").count(), 1)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "accepted")

    def test_a_command_still_in_flight_is_not_re_sent(self):
        self.dispatched()
        with self.committing():
            self.assertEqual(playout.advance(), "in_flight")
        self.assertEqual(len(self.commands), 1)

    def test_weather_and_amber_keep_emergency_priority(self):
        weather = self.weather_item(self.log, 2)
        self.engine.log_items = [self.song_a, weather, self.song_b]
        _execution, _publication, command = self.dispatched()
        self.insert(command)                                     # native arrives after the weather alert
        native = LogItem.objects.get(category__code="UrgentPA")
        self.assertEqual([item.id for item in self.queue_walk(3)], [weather.id, native.id, self.song_b.id])

    def test_a_weather_alert_arriving_later_still_goes_first(self):
        _execution, _publication, command = self.dispatched()
        self.insert(command)
        weather_track = self.song("Weather Alert", category=self.wxalert)
        self.engine._insert_urgent_next_inner("WxAlert")         # the unchanged category-only path
        native = LogItem.objects.get(category__code="UrgentPA")
        walked = self.queue_walk(3)
        self.assertEqual([item.track_id for item in walked], [weather_track.id, native.track_id, self.song_b.track_id])

    def test_with_a_dedication_follow_up_pending_the_forced_order_is_weather_native_follow_up(self):
        follow_up = self.song_b
        self.engine._forced_next_items = [follow_up]
        self.engine._queue_cursor = 2                            # the follow-up was already taken by the walk
        weather_track = self.song("Weather Alert", category=self.wxalert)
        self.engine._insert_urgent_next_inner("WxAlert")
        _execution, _publication, command = self.dispatched()
        self.insert(command)
        native = LogItem.objects.get(category__code="UrgentPA")
        self.assertEqual([item.track_id for item in self.engine._forced_next_items],
                         [weather_track.id, native.track_id, follow_up.track_id])

    def test_category_only_insert_urgent_never_picks_a_native_aggregate(self):
        legacy = self.song("Legacy OGRemote mix", category=self.urgentpa)    # the legacy UrgentPA Track
        _execution, publication, _command = self.dispatched()
        self.assertFalse(Track.objects.get(pk=publication.track_id).ready2air)
        self.engine._insert_urgent_next_inner("UrgentPA")
        self.assertEqual(self.engine.log_items[1].track_id, legacy.id)
        self.assertFalse(ActiveBroadcastInsertion.objects.exists())

    def test_a_refused_command_never_touches_the_queue(self):
        execution, publication, command = self.dispatched()
        cases = {"unknown execution": dict(command, active_execution=str(uuid.uuid4())),
                 "another track": dict(command, track=self.song_b.track_id),
                 "malformed": dict(command, attempt="not-a-uuid")}
        for label, payload in cases.items():
            with self.subTest(label):
                self.insert(payload)
                self.assertEqual(len(self.engine.log_items), 2)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "ready")
        self.assertTrue(publication)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class StalenessTests(Playout, TransactionTestCase):
    serialized_rollback = True

    """Every stale aggregate fails closed without skipping the next ordinary item."""

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)

    def inserted(self):
        execution, _publication, command = self.dispatched(actor=self.producer)
        self.engine._dispatch_engine_command(command)
        return execution, LogItem.objects.get(category__code="UrgentPA")

    def assert_skipped_to_song_b(self, execution):
        item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, self.song_b.id)                # the stale aggregate skipped, B not lost
        self.assertEqual(self.engine._queue_cursor, len(self.engine.log_items))   # ...and B handed out once
        self.assertEqual(playout.advance(), "suppressed")
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))

    def test_clear_after_render_before_publication(self):
        execution = self.ready_execution(actor=self.producer)
        services.clear_message(execution.composition_snapshot["components"][0]["message"], actor=self.admin)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "suppressed")
        self.assertEqual(playout.advance(), "none")
        self.assertFalse(list(self.library_root.glob("UrgentPA/am-*.flac")))

    def test_clear_after_publication_before_insertion(self):
        execution, _publication, command = self.dispatched(actor=self.producer)
        services.clear_message(execution.composition_snapshot["components"][0]["message"], actor=self.admin)
        self.engine._dispatch_engine_command(command)
        self.assertEqual(len(self.engine.log_items), 2)          # nothing inserted
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "suppressed")

    def test_clear_after_insertion_before_the_deck(self):
        execution, _native = self.inserted()
        services.clear_message(execution.composition_snapshot["components"][0]["message"], actor=self.admin)
        self.assert_skipped_to_song_b(execution)

    def test_replacement_after_insertion(self):
        execution, _native = self.inserted()
        self.real_active(self.producer, kind="urgent", seconds=1.0)          # supersedes the producer's message
        self.assert_skipped_to_song_b(execution)

    def test_expiry_before_playback(self):
        execution, _native = self.inserted()
        message = execution.composition_snapshot["components"][0]["message"]
        services.set_message_expiry(message, timezone.now() + timedelta(seconds=30), actor=self.admin)
        services.expire_due_messages(now=timezone.now() + timedelta(minutes=1))
        self.assert_skipped_to_song_b(execution)

    def test_generation_change_before_playback(self):
        execution, _native = self.inserted()
        services.set_limits(actor=self.admin, max_active_messages=10)
        self.assert_skipped_to_song_b(execution)

    def test_the_deck_open_authority_refuses_a_stale_aggregate(self):
        execution, native = self.inserted()
        services.set_limits(actor=self.admin, max_active_messages=10)
        with eng_module._deck_open_authority(native) as (track, reason):
            self.assertIsNone(track)
            self.assertIn("stale", reason)
        self.assertEqual(playout.advance(), "suppressed")         # the worker frees the slot

    def test_the_binding_authority_itself_suppresses_a_stale_aggregate(self):
        execution, native = self.inserted()
        services.set_limits(actor=self.admin, max_active_messages=10)
        decision = playout.authorize_open(native, eng_module.ENGINE_INSTANCE)
        self.assertFalse(decision.allowed)
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class DeckOpenAuthorityTests(Playout, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)
        self.execution, _publication, command = self.dispatched()
        self.engine._dispatch_engine_command(command)
        self.native = LogItem.objects.get(category__code="UrgentPA")

    def test_opening_claims_the_execution_and_ordinary_tracks_are_untouched(self):
        with eng_module._deck_open_authority(self.native) as (track, reason):
            self.assertIsNone(reason)
            self.assertEqual(track.pk, self.native.track_id)
        stored = ActiveBroadcastExecution.objects.get(pk=self.execution.pk)
        self.assertEqual((stored.state, stored.engine_instance), ("claimed", INSTANCE))
        self.assertIsNotNone(stored.claimed_at)
        with mock.patch("active_messages.playout._insertion_for", side_effect=AssertionError("query")):
            self.assertIsNone(playout.authorize_open(self.song_b, INSTANCE))   # ordinary: query-free, untouched

    def test_this_process_may_reopen_its_own_claim_another_may_not(self):
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)
        self.assertTrue(playout.authorize_open(self.native, INSTANCE).allowed)   # e.g. a seek continuation
        refused = playout.authorize_open(self.native, "another-engine-process")
        self.assertFalse(refused.allowed)

    def test_a_native_track_without_insertion_authority_never_opens(self):
        stray = LogItem.objects.create(playlist_log=self.log, position=50, scheduled_time=timezone.now(),
                                       track=self.native.track, category=self.urgentpa)
        self.assertFalse(playout.authorize_open(stray, INSTANCE).allowed)
        playable, reason = eng_module._log_item_playable(stray)
        self.assertFalse(playable)
        self.assertIn("insertion authority", reason)

    def test_the_gate_closing_after_insertion_stops_the_aggregate(self):
        services.set_native_airing(False, actor=self.admin)
        self.assertFalse(playout.authorize_open(self.native, INSTANCE).allowed)

    def test_an_engine_restart_rotates_an_unstarted_attempt_and_the_old_log_item_is_dead(self):
        old_attempt = ActiveBroadcastExecution.objects.get(pk=self.execution.pk).attempt_token
        with self.committing():
            playout.engine_started("restarted-engine-process")
        resent = self.commands[-1]
        self.assertNotEqual(resent["attempt"], str(old_attempt))
        self.assertFalse(playout.inspect(self.native, "restarted-engine-process").allowed)   # superseded
        self.engine._dispatch_engine_command(resent)
        self.assertEqual(ActiveBroadcastInsertion.objects.count(), 2)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).state, "accepted")

    def test_a_deck_that_could_not_open_re_sends_the_airing(self):
        with self.committing():
            playout.report_not_opened(self.native.pk)
        self.assertEqual(len(self.commands), 2)
        self.assertNotEqual(self.commands[-1]["attempt"], self.commands[0]["attempt"])

    def test_a_stranded_acceptance_is_re_sent_and_the_stranded_log_item_is_dead(self):
        # The engine's queue was replaced (play-now / reload / takeover): the
        # accepted LogItem will never be reached. After the bound it is re-sent.
        later = timezone.now() + timedelta(seconds=playout.ENGINE_HELD_RESEND_SECONDS + 1)
        with self.committing():
            self.assertEqual(playout.advance(now=later), "resent")
        self.assertFalse(playout.inspect(self.native, INSTANCE).allowed)          # superseded forever
        self.assertFalse(playout.authorize_open(self.native, INSTANCE).allowed)
        self.engine._dispatch_engine_command(self.commands[-1])
        self.assertEqual(ActiveBroadcastInsertion.objects.count(), 2)

    def test_attempts_are_bounded(self):
        from active_messages.tests.support import raw
        raw(ActiveBroadcastExecution).filter(pk=self.execution.pk).update(attempt_count=playout.MAX_ATTEMPTS)
        with self.committing():
            playout.report_not_opened(self.native.pk)
        stored = ActiveBroadcastExecution.objects.get(pk=self.execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "dispatch_exhausted"))
