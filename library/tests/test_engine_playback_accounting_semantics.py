"""P2 1.6 Phase B authoritative occurrence-accounting regressions."""

from __future__ import annotations

import tempfile
import threading
import inspect
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, close_old_connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import library.services.engine as eng_module
from library.models import (
    Artist,
    Category,
    CategoryKind,
    LogItem,
    PlayEvent,
    PlaylistLog,
    Track,
)
from library.services.engine import Deck, PlaybackEngine
from library.tests.test_engine_deck_lifecycle import (
    _make_real_engine,
    _wait_until,
    _write_wav,
)
from webrequests.models import SongRequest


class PlaybackAccountingFixture(TransactionTestCase):
    def setUp(self):
        self.kind = CategoryKind.objects.create(code="p216music", name="P2 1.6 Music")
        self.music = Category.objects.create(code="P216MUSIC", name="P2 1.6 Music", kind=self.kind)
        self.dedications, _created = Category.objects.get_or_create(
            code="Dedications",
            defaults={"name": "Dedications", "kind": self.kind},
        )
        self.artist = Artist.objects.create(name="P2 1.6 Artist")
        self.log = PlaylistLog.objects.create(
            date=date(2027, 9, 13), hour=10, status="approved"
        )
        self._serial = 0

    def make_occurrence(self, *, category=None, played_at=None):
        self._serial += 1
        category = category or self.music
        track = Track.objects.create(
            filepath=f"/tmp/p2-1.6-phase-b-{self._serial}.wav",
            filename=f"p2-1.6-phase-b-{self._serial}.wav",
            title=f"Occurrence {self._serial}",
            artist=self.artist,
            category=category,
            ready2air=True,
            duration_seconds=180.0,
            next_start_seconds=175.0,
        )
        item = LogItem.objects.create(
            playlist_log=self.log,
            position=self._serial,
            scheduled_time=timezone.now(),
            track=track,
            track_title=track.title,
            track_artist=self.artist.name,
            category=category,
            played_at=played_at,
        )
        return track, item

    def make_engine_deck(self, track, item, *, generation=1, eligible=True, reason=None):
        engine = object.__new__(PlaybackEngine)
        engine._lock = threading.RLock()
        deck = Deck(
            slot="A",
            track=track,
            log_item=item,
            pipeline=MagicMock(),
            mixer_pad=MagicMock(),
            generation=generation,
            air_start_eligible=eligible,
            continuation_reason=reason,
        )
        engine.decks = {"A": deck, "B": None}
        return engine, deck


class PlaybackClaimTests(PlaybackAccountingFixture):
    def test_claim_is_one_way_idempotent_and_is_not_air_evidence(self):
        track, item = self.make_occurrence()
        engine, _deck = self.make_engine_deck(track, item)

        self.assertTrue(engine._claim_playback_occurrence(item))
        first_claim = item.playback_claimed_at
        self.assertTrue(engine._claim_playback_occurrence(item))

        item.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual(item.playback_claimed_at, first_claim)
        self.assertIsNone(item.played_at)
        self.assertIsNone(track.last_played_at)
        self.assertEqual(track.play_count, 0)
        self.assertFalse(PlayEvent.objects.exists())

    def test_historical_rows_default_to_uncorrelated_null_fields(self):
        _track, item = self.make_occurrence()
        historical_event = PlayEvent.objects.create(
            track_title="Historical", started_at=timezone.now()
        )
        self.assertIsNone(item.playback_claimed_at)
        self.assertIsNone(historical_event.log_item_id_snapshot)


class AuthoritativeAirStartTransactionTests(PlaybackAccountingFixture):
    def setUp(self):
        super().setUp()
        self.track, self.item = self.make_occurrence()
        self.engine, self.deck = self.make_engine_deck(self.track, self.item)
        self.assertTrue(self.engine._claim_playback_occurrence(self.item))

    def call_start(self, at=None):
        at = at or timezone.now()
        self.engine._record_occurrence_air_start("A", 1, self.item.id, at)
        return at

    def test_one_transaction_uses_one_timestamp_for_every_semantic_write(self):
        request = SongRequest.objects.create(
            external_request_id="phase-b-request",
            track=self.track,
            status="scheduled",
            submitted_at=timezone.now() - timedelta(minutes=5),
            log_item=self.item,
        )
        air_started_at = self.call_start()

        self.item.refresh_from_db()
        self.track.refresh_from_db()
        request.refresh_from_db()
        event = PlayEvent.objects.get(log_item_id_snapshot=self.item.id)
        self.assertEqual(self.item.played_at, air_started_at)
        self.assertEqual(self.track.last_played_at, air_started_at)
        self.assertEqual(self.track.play_count, 1)
        self.assertEqual(request.fulfilled_at, air_started_at)
        self.assertEqual(request.resolved_at, air_started_at)
        self.assertEqual(event.started_at, air_started_at)
        self.assertEqual(self.deck.play_event_id, event.id)

    def test_duplicate_callback_is_a_noop(self):
        first = self.call_start()
        self.call_start(first + timedelta(seconds=10))

        self.item.refresh_from_db()
        self.track.refresh_from_db()
        event = PlayEvent.objects.get(log_item_id_snapshot=self.item.id)
        self.assertEqual(self.item.played_at, first)
        self.assertEqual(self.track.play_count, 1)
        self.assertEqual(event.started_at, first)
        self.assertEqual(PlayEvent.objects.count(), 1)

    def test_two_callbacks_racing_for_same_occurrence_count_once(self):
        barrier = threading.Barrier(2)
        errors = []
        at = timezone.now()

        def worker():
            try:
                close_old_connections()
                barrier.wait(timeout=5)
                self.engine._record_occurrence_air_start("A", 1, self.item.id, at)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.track.refresh_from_db()
        self.assertEqual(self.track.play_count, 1)
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=self.item.id).count(), 1)

    def test_stale_generation_is_rejected_before_database_work(self):
        replacement = Deck(
            "A", self.track, self.item, MagicMock(), MagicMock(),
            generation=2, air_start_eligible=True,
        )
        self.engine.decks["A"] = replacement
        self.call_start()
        self.item.refresh_from_db()
        self.assertIsNone(self.item.played_at)
        self.assertFalse(PlayEvent.objects.exists())

    def test_failure_rolls_back_log_track_request_and_event_together(self):
        request = SongRequest.objects.create(
            external_request_id="phase-b-rollback",
            track=self.track,
            status="scheduled",
            submitted_at=timezone.now(),
            log_item=self.item,
        )
        with patch.object(
            eng_module, "mark_song_requests_aired", side_effect=RuntimeError("forced failure")
        ), patch.object(GLib, "timeout_add_seconds", return_value=0), patch.object(
            eng_module, "emit_event"
        ):
            self.call_start()

        self.item.refresh_from_db()
        self.track.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNone(self.item.played_at)
        self.assertIsNone(self.track.last_played_at)
        self.assertEqual(self.track.play_count, 0)
        self.assertEqual(request.status, "scheduled")
        self.assertFalse(PlayEvent.objects.exists())

    def test_occurrence_snapshot_database_constraint_rejects_duplicate(self):
        self.call_start()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlayEvent.objects.create(
                    log_item_id_snapshot=self.item.id,
                    track_title="Duplicate",
                    started_at=timezone.now(),
                )


class DedicationAndContinuationTests(PlaybackAccountingFixture):
    def test_dedication_gets_actual_start_and_track_count_but_no_royalty_event(self):
        track, item = self.make_occurrence(category=self.dedications)
        engine, _deck = self.make_engine_deck(track, item)
        self.assertTrue(engine._claim_playback_occurrence(item))
        at = timezone.now()
        engine._record_occurrence_air_start("A", 1, item.id, at)

        item.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual(item.played_at, at)
        self.assertEqual(track.last_played_at, at)
        self.assertEqual(track.play_count, 1)
        self.assertFalse(PlayEvent.objects.exists())

    def test_seek_pause_and_auto_resume_generations_never_dispatch_new_play(self):
        track, item = self.make_occurrence(played_at=timezone.now())
        for reason in ("manual_seek_or_resume", "pause_resume", "auto_resume"):
            with self.subTest(reason=reason):
                engine, deck = self.make_engine_deck(
                    track, item, eligible=False, reason=reason
                )
                with patch.object(GLib, "idle_add") as idle_add:
                    engine._schedule_occurrence_air_start_from_probe(deck)
                idle_add.assert_not_called()

        track.refresh_from_db()
        self.assertEqual(track.play_count, 0)
        self.assertFalse(PlayEvent.objects.exists())

    def test_streaming_probe_handoff_is_in_memory_and_exactly_once(self):
        track, item = self.make_occurrence()
        engine, deck = self.make_engine_deck(track, item)
        with patch.object(GLib, "idle_add", return_value=42) as idle_add:
            engine._schedule_occurrence_air_start_from_probe(deck)
            engine._schedule_occurrence_air_start_from_probe(deck)

        idle_add.assert_called_once()
        (
            callback, slot, generation, log_item_id, captured_at,
            duration_generation_id,
        ) = idle_add.call_args.args
        self.assertEqual(callback, engine._record_occurrence_air_start)
        self.assertEqual((slot, generation, log_item_id), ("A", 1, item.id))
        self.assertTrue(timezone.is_aware(captured_at))
        self.assertEqual(duration_generation_id, deck.duration_generation_id)
        self.assertEqual(deck.air_start_dispatch_source_id, 42)

        source = inspect.getsource(
            PlaybackEngine._schedule_occurrence_air_start_from_probe
        )
        self.assertNotIn(".objects", source)
        self.assertNotIn("emit_event", source)
        self.assertNotIn("write_text", source)
        self.assertNotIn("requests.", source)


class RealBoundaryAccountingIntegrationTests(PlaybackAccountingFixture):
    def make_real_engine(self):
        engine = _make_real_engine()
        engine.__dict__.pop("_claim_playback_occurrence")
        engine.__dict__.pop("_schedule_occurrence_air_start_from_probe")
        engine.__dict__.pop("_persist_deck_duration")
        engine.__dict__.pop("_schedule_continuation_segment_start")

        def cleanup_engine():
            engine.main_pipeline.set_state(Gst.State.NULL)
            for coordinator in engine._deck_teardowns.values():
                coordinator.stop()

        self.addCleanup(cleanup_engine)
        return engine

    def test_never_real_deck_retires_as_claimed_without_air_accounting(self):
        temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-phase-b-never-real.")
        self.addCleanup(temp_dir.cleanup)
        media_path = Path(temp_dir.name) / "never-real.wav"
        _write_wav(media_path, frames=44100)
        track, item = self.make_occurrence()
        track.filepath = str(media_path)
        track.filename = media_path.name
        track.save(update_fields=["filepath", "filename"])
        request = SongRequest.objects.create(
            external_request_id="phase-b-never-real",
            track=track,
            status="scheduled",
            submitted_at=timezone.now(),
            log_item=item,
        )

        engine = self.make_real_engine()
        # Leave the parent pipeline stopped, so no real-content buffer can
        # cross concat.src, and retire the fully constructed generation.
        deck = engine._create_deck("A", item)
        self.assertIsNotNone(deck)
        self.assertTrue(engine._remove_deck(deck))

        item.refresh_from_db()
        track.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNotNone(item.playback_claimed_at)
        self.assertIsNone(item.played_at)
        self.assertIsNone(track.last_played_at)
        self.assertEqual(track.play_count, 0)
        self.assertEqual(request.status, "scheduled")
        self.assertIsNone(request.fulfilled_at)
        self.assertFalse(PlayEvent.objects.exists())

    def test_claim_precedes_real_output_and_phase_a_boundary_commits_once(self):
        temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-phase-b-boundary.")
        self.addCleanup(temp_dir.cleanup)
        media_path = Path(temp_dir.name) / "real.wav"
        _write_wav(media_path, frames=44100)
        track, item = self.make_occurrence()
        track.filepath = str(media_path)
        track.filename = media_path.name
        track.save(update_fields=["filepath", "filename"])
        request = SongRequest.objects.create(
            external_request_id="phase-b-real-boundary",
            track=track,
            status="scheduled",
            submitted_at=timezone.now(),
            log_item=item,
        )

        engine = self.make_real_engine()
        # The shared topology fixture stubs Phase B persistence so older
        # lifecycle tests remain hardware/DB-independent. This integration
        # test deliberately restores the production methods.
        deck = engine._create_deck("A", item)
        self.assertIsNotNone(deck)
        item.refresh_from_db()
        track.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNotNone(item.playback_claimed_at)
        self.assertIsNone(item.played_at)
        self.assertEqual(track.play_count, 0)
        self.assertEqual(request.status, "scheduled")
        self.assertFalse(PlayEvent.objects.exists())

        engine.main_pipeline.set_state(Gst.State.PLAYING)

        def committed():
            return PlayEvent.objects.filter(log_item_id_snapshot=item.id).exists()

        self.assertTrue(_wait_until(committed, timeout=5.0), deck.milestone_snapshot())
        item.refresh_from_db()
        track.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNotNone(item.played_at)
        self.assertEqual(track.play_count, 1)
        self.assertEqual(request.status, "fulfilled")
        event = PlayEvent.objects.get(log_item_id_snapshot=item.id)
        self.assertEqual(track.last_played_at, item.played_at)
        self.assertEqual(request.fulfilled_at, item.played_at)
        self.assertEqual(event.started_at, item.played_at)
        self.assertTrue(
            _wait_until(
                lambda: PlayEvent.objects.filter(
                    id=event.id, ended_at__isnull=False,
                    duration_played_seconds__isnull=False,
                ).exists(),
                timeout=5.0,
            ),
            deck.milestone_snapshot(),
        )
        event.refresh_from_db()
        self.assertGreaterEqual(event.duration_played_seconds, 0.0)
        segment = event.duration_segments.get()
        self.assertEqual(segment.started_at, event.started_at)
        self.assertEqual(segment.evidence_state, "complete")
        self.assertAlmostEqual(
            event.duration_played_seconds,
            segment.confirmed_duration_seconds,
        )

    def test_auto_resume_with_null_played_at_is_conservative_and_diagnostic(self):
        temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-phase-b-auto-resume.")
        self.addCleanup(temp_dir.cleanup)
        media_path = Path(temp_dir.name) / "resume.wav"
        _write_wav(media_path, frames=44100)
        track, item = self.make_occurrence()
        track.filepath = str(media_path)
        track.filename = media_path.name
        track.save(update_fields=["filepath", "filename"])
        engine = self.make_real_engine()
        engine._resume_hint = {
            "track_id": track.id,
            "log_item_id": item.id,
            "position": 0.5,
        }

        with patch.object(eng_module, "emit_event") as emit:
            deck = engine._create_deck("A", item)

        self.assertIsNotNone(deck)
        self.assertFalse(deck.air_start_eligible)
        self.assertEqual(deck.continuation_reason, "auto_resume")
        item.refresh_from_db()
        track.refresh_from_db()
        self.assertIsNotNone(item.playback_claimed_at)
        self.assertIsNone(item.played_at)
        self.assertEqual(track.play_count, 0)
        self.assertFalse(PlayEvent.objects.exists())
        self.assertTrue(
            any(
                call.kwargs.get("title")
                == "Auto-resume occurrence lacks authoritative air start"
                for call in emit.call_args_list
            )
        )
