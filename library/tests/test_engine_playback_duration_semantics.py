"""P2 1.6 Phase C durable, generation-safe duration regressions."""

from datetime import date, timedelta
import inspect
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
Gst.init(None)

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogItem, PlayEvent, PlayEventSegment,
    PlaylistLog, Track,
)
from library.services.engine import Deck, PlaybackEngine
from library.services.royalty_reports import generate_raw_csv, generate_summary


class DurationFixture(TransactionTestCase):
    def setUp(self):
        self.kind = CategoryKind.objects.create(code="phasec", name="Music")
        self.category = Category.objects.create(
            code="PHASEC", name="Phase C", kind=self.kind
        )
        self.artist = Artist.objects.create(name="Phase C Artist")
        self.log = PlaylistLog.objects.create(
            date=date(2027, 1, 2), hour=12, status="approved"
        )
        self.track = Track.objects.create(
            filepath="/tmp/phase-c.wav", filename="phase-c.wav",
            title="Phase C Track", artist=self.artist, category=self.category,
            ready2air=True, duration_seconds=180, next_start_seconds=175,
        )
        self.item = LogItem.objects.create(
            playlist_log=self.log, position=1, scheduled_time=timezone.now(),
            track=self.track, track_title=self.track.title,
            track_artist=self.artist.name, category=self.category,
        )
        self.engine = object.__new__(PlaybackEngine)
        self.engine._lock = __import__("threading").RLock()
        self.deck = self.make_deck(1, eligible=True)
        self.engine.decks = {"A": self.deck, "B": None}
        self.assertTrue(self.engine._claim_playback_occurrence(self.item))

    def make_deck(self, generation, *, eligible=False, reason="manual_seek"):
        return Deck(
            "A", self.track, self.item, MagicMock(), MagicMock(),
            generation=generation, air_start_eligible=eligible,
            continuation_reason=reason,
        )

    def start_fresh(self, at=None):
        at = at or timezone.now()
        self.engine._record_occurrence_air_start(
            "A", self.deck.generation, self.item.id, at,
            self.deck.duration_generation_id,
        )
        return PlayEvent.objects.get(log_item_id_snapshot=self.item.id)

    @staticmethod
    def add_duration(deck, seconds):
        buf = Gst.Buffer.new()
        buf.duration = int(seconds * Gst.SECOND)
        deck.mark_program_buffer(buf)

    def start_continuation(self, generation, reason="manual_seek"):
        deck = self.make_deck(generation, reason=reason)
        self.engine.decks["A"] = deck
        at = timezone.now()
        deck.activate_duration_segment(at, reason)
        self.engine._record_continuation_segment_start(
            "A", generation, self.item.id, deck.duration_generation_id,
            at, reason,
        )
        return deck


class OrdinaryDurationTests(DurationFixture):
    def test_uninterrupted_play_is_one_segment_and_one_aggregate(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 12.25)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event.refresh_from_db()
        segment = event.duration_segments.get()
        self.assertAlmostEqual(segment.confirmed_duration_seconds, 12.25)
        self.assertAlmostEqual(event.duration_played_seconds, 12.25)
        self.assertEqual(segment.evidence_state, "complete")
        self.assertEqual(event.duration_evidence_state, "complete")
        self.assertIsNotNone(event.ended_at)

    def test_primer_and_prestart_buffers_are_excluded(self):
        self.add_duration(self.deck, 0.3)
        self.start_fresh()
        self.add_duration(self.deck, 1.0)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        self.assertAlmostEqual(
            PlayEvent.objects.get().duration_played_seconds, 1.0
        )

    def test_claimed_but_never_aired_has_no_event_or_segment(self):
        self.assertFalse(PlayEvent.objects.exists())
        self.assertFalse(PlayEventSegment.objects.exists())

    def test_very_short_complete_play_remains_truthful(self):
        self.start_fresh()
        self.add_duration(self.deck, 0.08)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event = PlayEvent.objects.get()
        self.assertAlmostEqual(event.duration_played_seconds, 0.08)
        today = timezone.localdate()
        summary, _ = generate_summary(today, today)
        self.assertIn("below 30s (excluded): 1", summary)

    def test_buffers_after_detach_boundary_do_not_advance_duration(self):
        self.start_fresh()
        self.add_duration(self.deck, 2)
        self.deck.duration_snapshot(freeze=True, ended_at=timezone.now())
        self.add_duration(self.deck, 9)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        self.assertAlmostEqual(PlayEvent.objects.get().duration_played_seconds, 2)


class ContinuationDurationTests(DurationFixture):
    def test_forward_then_backward_seek_accumulates_segments_not_gaps(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 10)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="seek_replaced",
        )
        second = self.start_continuation(2, "manual_forward_seek")
        self.add_duration(second, 4)
        self.engine._persist_deck_duration(
            second, close_segment=True, occurrence_terminal=False,
            termination_reason="seek_replaced",
        )
        third = self.start_continuation(3, "manual_backward_seek")
        self.add_duration(third, 7)
        self.engine._persist_deck_duration(
            third, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event.refresh_from_db()
        self.assertEqual(event.duration_segments.count(), 3)
        self.assertAlmostEqual(event.duration_played_seconds, 21)
        self.assertEqual(PlayEvent.objects.count(), 1)
        self.item.refresh_from_db()
        self.track.refresh_from_db()
        self.assertEqual(self.track.play_count, 1)

    def test_pause_and_clean_restart_gaps_do_not_count(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 6)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="paused",
        )
        resumed = self.start_continuation(2, "pause_resume")
        self.add_duration(resumed, 5)
        self.engine._persist_deck_duration(
            resumed, close_segment=True, occurrence_terminal=False,
            termination_reason="clean_shutdown",
        )
        restarted = self.start_continuation(3, "auto_resume")
        self.add_duration(restarted, 3)
        self.engine._persist_deck_duration(
            restarted, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event.refresh_from_db()
        self.assertAlmostEqual(event.duration_played_seconds, 14)
        self.assertEqual(event.duration_segments.count(), 3)
        self.assertEqual(PlayEvent.objects.count(), 1)

    def test_preconfirmation_buffer_and_stale_callback_cannot_start_segment(self):
        event = self.start_fresh()
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="seek_replaced",
        )
        replacement = self.make_deck(2, reason="auto_resume")
        self.engine.decks["A"] = replacement
        self.add_duration(replacement, 9)  # gated/pre-confirmation: inactive
        stale_uuid = self.deck.duration_generation_id
        self.engine._record_continuation_segment_start(
            "A", 1, self.item.id, stale_uuid, timezone.now(), "stale"
        )
        replacement.activate_duration_segment(timezone.now(), "auto_resume")
        self.engine._record_continuation_segment_start(
            "A", 2, self.item.id, replacement.duration_generation_id,
            replacement.duration_segment_started_at, "auto_resume",
        )
        self.add_duration(replacement, 2)
        self.engine._persist_deck_duration(
            replacement, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event.refresh_from_db()
        self.assertEqual(event.duration_segments.count(), 2)
        self.assertAlmostEqual(event.duration_played_seconds, 2)

    def test_occurrence_mismatch_cannot_attach_to_old_event(self):
        self.start_fresh()
        other = LogItem.objects.create(
            playlist_log=self.log, position=2, scheduled_time=timezone.now(),
            track=self.track, track_title=self.track.title,
            track_artist=self.artist.name, category=self.category,
            playback_claimed_at=timezone.now(), played_at=timezone.now(),
        )
        deck = Deck(
            "A", self.track, other, MagicMock(), MagicMock(), generation=2,
            continuation_reason="auto_resume",
        )
        self.engine.decks["A"] = deck
        deck.activate_duration_segment(timezone.now(), "auto_resume")
        self.engine._record_continuation_segment_start(
            "A", 2, other.id, deck.duration_generation_id,
            deck.duration_segment_started_at, "auto_resume",
        )
        self.assertEqual(PlayEventSegment.objects.count(), 1)

    def test_tunein_identity_is_stable_across_continuations_and_changes_next_song(self):
        first = self.start_fresh()
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="seek_replaced",
        )
        resumed = self.start_continuation(2, "auto_resume")
        self.engine._persist_deck_duration(
            resumed, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        self.assertEqual(
            PlayEvent.objects.order_by("-started_at").first().id, first.id
        )
        self.assertEqual(PlayEvent.objects.count(), 1)

        next_track = Track.objects.create(
            filepath="/tmp/phase-c-next.wav", filename="phase-c-next.wav",
            title="Next Track", artist=self.artist, category=self.category,
            ready2air=True, duration_seconds=120, next_start_seconds=115,
        )
        next_item = LogItem.objects.create(
            playlist_log=self.log, position=2, scheduled_time=timezone.now(),
            track=next_track, track_title=next_track.title,
            track_artist=self.artist.name, category=self.category,
        )
        next_deck = Deck(
            "A", next_track, next_item, MagicMock(), MagicMock(), generation=3,
            air_start_eligible=True,
        )
        self.engine.decks["A"] = next_deck
        self.assertTrue(self.engine._claim_playback_occurrence(next_item))
        self.engine._record_occurrence_air_start(
            "A", 3, next_item.id, timezone.now(), next_deck.duration_generation_id
        )
        latest = PlayEvent.objects.order_by("-started_at", "-id").first()
        self.assertNotEqual(latest.id, first.id)
        self.assertEqual(PlayEvent.objects.count(), 2)


class CrashAndTransactionTests(DurationFixture):
    def test_crash_retains_checkpoint_marks_uncertainty_and_same_resume_continues(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 31)
        self.engine._persist_deck_duration(self.deck)
        self.engine._resume_hint = {
            "track_id": self.track.id, "log_item_id": self.item.id,
            "position": 40,
        }
        self.engine._reconcile_playback_duration_state()
        event.refresh_from_db()
        first = event.duration_segments.get()
        self.assertEqual(first.evidence_state, "interrupted")
        self.assertIsNone(first.ended_at)
        self.assertAlmostEqual(first.confirmed_duration_seconds, 31)
        self.assertIsNone(event.ended_at)
        resumed = self.start_continuation(2, "auto_resume")
        self.add_duration(resumed, 2)
        self.engine._persist_deck_duration(
            resumed, close_segment=True, occurrence_terminal=True,
            termination_reason="natural_eos",
        )
        event.refresh_from_db()
        self.assertEqual(event.duration_segments.count(), 2)
        self.assertEqual(event.duration_evidence_state, "interrupted")
        self.assertAlmostEqual(event.duration_played_seconds, 33)
        self.assertIsNone(event.ended_at)

    def test_crash_different_occurrence_never_invents_end_or_downtime(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 11)
        self.engine._persist_deck_duration(self.deck)
        self.engine._resume_hint = {"track_id": -1, "log_item_id": -1, "position": 99}
        self.engine._reconcile_playback_duration_state()
        event.refresh_from_db()
        self.assertAlmostEqual(event.duration_played_seconds, 11)
        self.assertEqual(event.duration_evidence_state, "interrupted")
        self.assertIsNone(event.ended_at)

    def test_clean_closed_segment_without_matching_hint_finalizes_at_known_end(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 4)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="clean_shutdown",
        )
        segment_end = PlayEventSegment.objects.get().ended_at
        self.engine._resume_hint = None
        self.engine._reconcile_playback_duration_state()
        event.refresh_from_db()
        self.assertEqual(event.ended_at, segment_end)
        self.assertEqual(event.duration_evidence_state, "complete")

    def test_clean_hint_for_missing_loaded_occurrence_does_not_hold_event_open(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 4)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=False,
            termination_reason="clean_shutdown",
        )
        self.engine._resume_hint = {
            "track_id": self.track.id,
            "log_item_id": self.item.id,
            "position": 20,
        }
        self.engine.log_items = []
        self.engine._forced_next_items = []
        self.engine._reconcile_playback_duration_state()
        event.refresh_from_db()
        self.assertEqual(event.duration_evidence_state, "complete")
        self.assertIsNotNone(event.ended_at)

    def test_segment_and_aggregate_update_roll_back_together(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 8)
        with patch.object(
            eng_module.PlayEvent.objects, "filter", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(self.engine._persist_deck_duration(self.deck))
        event.refresh_from_db()
        segment = event.duration_segments.get()
        self.assertEqual(segment.confirmed_duration_seconds, 0)
        self.assertEqual(event.duration_played_seconds, None)

    def test_checkpoint_is_absolute_idempotent_and_db_failure_is_nonfatal(self):
        event = self.start_fresh()
        self.add_duration(self.deck, 5)
        self.assertTrue(self.engine._checkpoint_playback_durations())
        self.assertTrue(self.engine._checkpoint_playback_durations())
        event.refresh_from_db()
        self.assertAlmostEqual(event.duration_played_seconds, 5)
        self.add_duration(self.deck, 1)
        with patch.object(
            eng_module.PlayEvent.objects, "filter", side_effect=RuntimeError("db down")
        ):
            self.assertTrue(self.engine._checkpoint_playback_durations())

    def test_checkpoint_is_low_rate_and_streaming_accumulator_has_no_orm(self):
        self.assertEqual(eng_module.PLAYBACK_DURATION_CHECKPOINT_SECONDS, 5)
        source = inspect.getsource(Deck.mark_program_buffer)
        self.assertNotIn(".objects", source)
        self.assertNotIn("save(", source)
        self.assertNotIn("emit_event", source)

        self.start_fresh()
        self.add_duration(self.deck, 3)
        stale = self.deck
        replacement = self.make_deck(2)
        self.engine.decks["A"] = replacement
        self.engine._checkpoint_playback_durations()
        self.assertEqual(
            PlayEventSegment.objects.get(generation_id=stale.duration_generation_id)
            .confirmed_duration_seconds,
            0,
        )


class ReportingAndHistoryTests(DurationFixture):
    def make_terminal(self, seconds, *, interrupted=False):
        event = self.start_fresh()
        self.add_duration(self.deck, seconds)
        self.engine._persist_deck_duration(
            self.deck, close_segment=True, occurrence_terminal=not interrupted,
            termination_reason="crash" if interrupted else "natural_eos",
            interrupted=interrupted,
        )
        return event

    def test_uncertain_threshold_reporting_and_raw_audit_fields(self):
        event = self.make_terminal(29.5, interrupted=True)
        today = timezone.localdate()
        summary, _ = generate_summary(today, today)
        self.assertIn("threshold ambiguous, excluded from automatic export): 1", summary)
        raw, _ = generate_raw_csv(today, today)
        self.assertIn("log_item_id_snapshot,duration_evidence_state,segment_count", raw)
        self.assertIn("interrupted", raw)
        event.refresh_from_db()
        self.assertEqual(event.duration_evidence_state, "interrupted")

    def test_complete_29x_is_excluded_and_complete_30x_is_included(self):
        self.make_terminal(29.9)
        today = timezone.localdate()
        summary, _ = generate_summary(today, today)
        self.assertIn("Total plays (post-threshold): 0", summary)
        self.assertIn("below 30s (excluded): 1", summary)

        # Change the absolute evidence total to the other threshold side;
        # the report policy remains query-time based.
        segment = PlayEventSegment.objects.get()
        segment.confirmed_duration_seconds = 30.1
        segment.save(update_fields=["confirmed_duration_seconds"])
        event = PlayEvent.objects.get()
        event.duration_played_seconds = 30.1
        event.save(update_fields=["duration_played_seconds"])
        summary, _ = generate_summary(today, today)
        self.assertIn("Total plays (post-threshold): 1", summary)

    def test_interrupted_30x_is_definitely_qualified(self):
        self.make_terminal(30.1, interrupted=True)
        today = timezone.localdate()
        summary, _ = generate_summary(today, today)
        self.assertIn("Total plays (post-threshold): 1", summary)
        self.assertIn("definitely qualified, included): 1", summary)

    def test_historical_event_stays_legacy_without_fabricated_segment(self):
        historical = PlayEvent.objects.create(
            track_title="Historical", started_at=timezone.now() - timedelta(days=1),
            duration_played_seconds=12,
        )
        self.assertEqual(historical.duration_evidence_state, "legacy")
        self.assertFalse(historical.duration_segments.exists())
