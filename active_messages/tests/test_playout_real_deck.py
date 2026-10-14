"""D2B end to end on the REAL existing A/B deck (real GStreamer, real
PostgreSQL), no mock anywhere in the airing chain:

    D2A aggregate (real render) -> UrgentPA Track (real ingest/analysis)
    -> the engine's own insert_urgent splice -> _next_queue_item
    -> _create_deck (deck-open authority: CLAIMED) -> real first post-primer
    buffer (LogItem.played_at + PlayEvent) -> STARTED -> real EOS (terminal
    ledger, termination_reason "eos") -> COMPLETED -> D1 cadence.

The engine is the shared real-topology fixture (test_engine_deck_lifecycle)
with Phase B/C persistence restored, as the existing real accounting
integration tests do. The program output is a fakesink: no audio device."""
import unittest
import unittest.mock
from datetime import timedelta

import gi
from django.test import TransactionTestCase

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import library.services.engine as eng_module  # noqa: E402
from active_messages import playout, services  # noqa: E402
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage  # noqa: E402
from library.models import LogItem, PlayEvent  # noqa: E402
from library.services import royalty_reports  # noqa: E402
from library.tests.test_engine_deck_lifecycle import _make_real_engine, _wait_until  # noqa: E402
from production.tests.support import ffmpeg_available  # noqa: E402

from .d2b_support import Playout  # noqa: E402


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RealDeckTests(Playout, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        self.engine = engine = _make_real_engine()
        for production_method in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                                  "_persist_deck_duration", "_schedule_continuation_segment_start"):
            engine.__dict__.pop(production_method)
        engine.current_log, engine.log_items, engine._queue_cursor = self.log, [self.song_a, self.song_b], 1
        engine._forced_next_items, engine._urgent_retry_counts = [], {}

        def cleanup():
            engine.main_pipeline.set_state(Gst.State.NULL)
            for coordinator in engine._deck_teardowns.values():
                coordinator.stop()
        self.addCleanup(cleanup)

    def on_air(self, seconds=1.0):
        """Dispatch a real aggregate, let the engine insert it, take it from
        the queue and open it on the ordinary deck."""
        execution, publication, command = self.dispatched(seconds=seconds)
        self.engine._dispatch_engine_command(command)
        item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.track_id, publication.track_id)
        deck = self.engine._create_deck("A", item)
        self.assertIsNotNone(deck)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "claimed")
        self.assertIsNone(LogItem.objects.get(pk=item.pk).played_at)        # a claim is not air evidence
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        return execution, item, deck

    def state(self, execution):
        return ActiveBroadcastExecution.objects.get(pk=execution.pk).state

    def test_the_aggregate_airs_on_the_ordinary_deck_and_completes_with_d1_cadence(self):
        execution, item, deck = self.on_air()
        self.assertTrue(_wait_until(lambda: self.state(execution) in ("started", "completed"), timeout=10.0),
                        deck.milestone_snapshot())
        event = PlayEvent.objects.get(log_item_id_snapshot=item.pk)
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual(stored.started_at, event.started_at)                 # the deck's own first real buffer
        self.assertEqual(stored.started_at, LogItem.objects.get(pk=item.pk).played_at)
        self.assertTrue(_wait_until(lambda: self.state(execution) == "completed", timeout=15.0),
                        deck.milestone_snapshot())
        event.refresh_from_db()
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        segment = event.duration_segments.order_by("-started_at").first()
        self.assertEqual((event.duration_evidence_state, segment.termination_reason), ("complete", "eos"))
        self.assertEqual(stored.ended_at, event.ended_at)
        self.assertAlmostEqual(float(stored.confirmed_duration_seconds), 1.0, delta=0.2)
        message = ActiveBroadcastMessage.objects.get(pk=stored.composition_snapshot["components"][0]["message"])
        self.assertEqual(message.last_completed_at, stored.ended_at)
        self.assertEqual(message.next_due_at, stored.ended_at + timedelta(seconds=message.replay_interval_seconds))
        # Playlist continuity: the next ordinary item is next, exactly once.
        following, _forced = self.engine._next_queue_item()
        self.assertEqual(following.id, self.song_b.id)
        # Accounting: the existing nonmusic classification of UrgentPA (kind Spot) -- not a music performance.
        self.assertEqual(event.category_kind, self.urgentpa.kind.name)
        self.assertNotEqual(event.category_kind, "Music")
        self.assertFalse(royalty_reports._music_qs(event.started_at.date(), event.started_at.date(),
                                                   apply_threshold=False).filter(pk=event.pk).exists())

    def test_a_started_airing_cut_by_an_engine_stop_is_interrupted_and_not_replayed_at_once(self):
        # Real-time output (the fixture's fakesink otherwise runs unsynchronized
        # and a short aggregate reaches EOS in milliseconds) so the stop lands
        # mid-airing.
        self.engine.main_pipeline.get_by_name("sink").set_property("sync", True)
        execution, item, deck = self.on_air(seconds=3.0)
        self.assertTrue(_wait_until(lambda: self.state(execution) == "started", timeout=10.0),
                        deck.milestone_snapshot())
        self.engine.stop()                                                     # the process goes away mid-airing
        playout.engine_started("the-next-engine-process")
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual(stored.state, "interrupted")
        self.assertGreaterEqual(stored.ended_at, stored.started_at)
        message = ActiveBroadcastMessage.objects.get(pk=stored.composition_snapshot["components"][0]["message"])
        self.assertIsNone(message.last_completed_at)
        self.assertEqual(message.next_due_at, stored.ended_at + timedelta(seconds=message.replay_interval_seconds))
        self.assertIsNone(services.materialize_due_execution())               # no immediate replay
        # ...and the next engine never resumes the cut aggregate.
        self.assertFalse(playout.authorize_open(item, "the-next-engine-process").allowed)

    def test_the_engines_ordinary_hooks_drive_the_evidence_without_the_worker(self):
        execution, _item, deck = self.on_air()
        with unittest.mock.patch("active_messages.worker.run_once", side_effect=AssertionError("no worker here")):
            self.assertTrue(_wait_until(lambda: self.state(execution) == "completed", timeout=15.0),
                            deck.milestone_snapshot())
        self.assertIsNotNone(eng_module.ENGINE_INSTANCE)
