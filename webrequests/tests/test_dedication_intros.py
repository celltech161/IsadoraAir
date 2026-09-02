"""Regression coverage for spoken dedication intros -- see the approved
plan (Design sections 1-7) for the full narrative. Five rounds of design
review found real bugs in: a self-defeating reinsertion loop, a rollover
race that was only narrowed rather than closed, a cursor bug that
returned the wrong item from the splice, missing forced-item visibility
in the crossfade lookahead and the queue preview (which the web-request
reconciliation command also reads), an incomplete restart-recovery story
that let an intro air without its song in a real if narrow case, a
synthesis winner-selection query whose per-cycle dedup didn't survive
the winning request's own row dropping out of the query once
synthesized, missing bounded-lock discipline on the engine's single
GLib thread, and whole-function fail-open gaps that could have let a
bug in this feature stop a requested song from playing at all. This
suite has one or more tests per confirmed concern.

TransactionTestCase throughout -- several tests spawn real threads that
hold row locks, same reasoning as test_request_scheduling_lifecycle.py."""
import inspect
import json
import os
import tempfile
import threading
import time
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.db.utils import OperationalError
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from isadoraair.tts.models import StationTTSVoice
from library.models import (
    Artist, Category, CategoryKind, LogItem, PlaylistLog, Track, VoiceTrack, VoiceTrackConfig,
)
from webrequests.admin import SongRequestAdmin
from webrequests.management.commands.generate_dedication_intros import DEDICATION_LOCK_KEY
from webrequests.management.commands.refresh_song_request_statuses import Command as RefreshCommand
from webrequests.models import SongRequest, WebRequestConfig
from webrequests.services import build_dedication_intro_text, synthesize_dedication_intro
import webrequests.services as services_module

from webrequests.tests.test_request_scheduling_lifecycle import WebRequestFixtureMixin


def make_engine_stand_in():
    """A bare PlaybackEngine instance, bypassing __init__ -- same
    technique as test_request_scheduling_lifecycle.make_engine_stand_in,
    extended with the dedication-feature's own instance attributes."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._next_triggered = False
    obj._deck_bin_map = {}
    obj._forced_next_items = []
    obj._urgent_retry_counts = {}
    obj.current_log = None
    obj.log_items = []
    obj._queue_cursor = 0
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    return obj


class DedicationFixtureMixin(WebRequestFixtureMixin):
    def setUp(self):
        super().setUp()
        spot_kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        self.dedication_category, _ = Category.objects.get_or_create(
            code="Dedications", defaults={"name": "Dedications", "kind": spot_kind},
        )

    def make_dedication_track(self, title="Dedication Intro", duration=6.0):
        self._track_counter += 1
        real_path = Path(self._tmpdir.name) / f"dedi-{self._track_counter}.flac"
        real_path.touch()
        return Track.objects.create(
            title=title, artist=self.artist, category=self.dedication_category,
            ready2air=True, filepath=str(real_path),
            duration_seconds=duration, next_start_seconds=duration, cue_in_seconds=0,
        )

    def make_dedication_item(self, log, position, track, scheduled_time=None, played_at=None):
        """Unlike WebRequestFixtureMixin.make_item, hardcoded to
        self.category, this sets the LogItem's own category to
        Dedications -- required for realism wherever
        _maybe_insert_dedication_intro_inner's "kind.code == music" gate
        or category-based checks elsewhere matter."""
        return LogItem.objects.create(
            playlist_log=log, position=position,
            scheduled_time=scheduled_time or timezone.now(),
            track=track, category=self.dedication_category, played_at=played_at,
        )

    def make_engine_stand_in(self, current_log=None, log_items=None):
        stand_in = make_engine_stand_in()
        stand_in.current_log = current_log
        stand_in.log_items = log_items if log_items is not None else []
        return stand_in


# ---------------------------------------------------------------------
# 1. Text template
# ---------------------------------------------------------------------
class DedicationTextTemplateTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_full_dedication_with_name_and_message(self):
        track = self.make_track(title="Free Fallin'")
        text = build_dedication_intro_text(track, "Justin", "for my late night drive")
        self.assertEqual(
            text,
            "Now here's Free Fallin' by Test Artist, for my late night drive. "
            "Thanks Justin for your dedication.",
        )

    def test_request_with_name_no_message(self):
        track = self.make_track(title="Free Fallin'")
        text = build_dedication_intro_text(track, "Justin", "")
        self.assertEqual(text, "Now here's Free Fallin' by Test Artist. Thanks Justin for your request.")

    def test_whitespace_and_newlines_collapsed(self):
        track = self.make_track()
        text = build_dedication_intro_text(track, "Justin", "  hello \n  world  ")
        self.assertIn(", hello world.", text)

    def test_existing_terminal_punctuation_not_doubled(self):
        track = self.make_track()
        text = build_dedication_intro_text(track, "Justin", "rock on!")
        self.assertIn("rock on!", text)
        self.assertNotIn("rock on!.", text)

    def test_name_stripped_of_surrounding_whitespace(self):
        track = self.make_track()
        text = build_dedication_intro_text(track, "  Justin  ", "")
        self.assertTrue(text.endswith("Thanks Justin for your request."))

    def test_blank_name_drops_closing_sentence(self):
        track = self.make_track()
        text = build_dedication_intro_text(track, "", "hi")
        self.assertEqual(text, "Now here's Test Song by Test Artist, hi.")

    def test_message_of_only_whitespace_treated_as_no_message(self):
        track = self.make_track()
        text = build_dedication_intro_text(track, "Justin", "   ")
        self.assertEqual(text, "Now here's Test Song by Test Artist. Thanks Justin for your request.")

    def test_feat_in_title_normalized_to_featuring(self):
        track = self.make_track(title="Free Fallin' (feat. Stevie Nicks)")
        text = build_dedication_intro_text(track, "Justin", "")
        self.assertIn("Free Fallin' (featuring Stevie Nicks)", text)
        self.assertNotIn("feat.", text)

    def test_feat_in_artist_normalized_to_featuring(self):
        artist = Artist.objects.create(name="Tom Petty feat. Stevie Nicks")
        track = Track.objects.create(
            title="Free Fallin'", artist=artist, category=self.category,
            ready2air=True, filepath=str(Path(self._tmpdir.name) / "feat-artist.mp3"),
            duration_seconds=180.0,
        )
        Path(track.filepath).touch()
        text = build_dedication_intro_text(track, "Justin", "")
        self.assertIn("by Tom Petty featuring Stevie Nicks", text)

    def test_feat_normalization_is_case_insensitive_and_no_period_variant_untouched(self):
        track = self.make_track(title="Song (Feat. Someone)")
        text = build_dedication_intro_text(track, "Justin", "")
        self.assertIn("(featuring Someone)", text)

        track2 = self.make_track(title="Incredible Feat")
        text2 = build_dedication_intro_text(track2, "Justin", "")
        self.assertIn("Incredible Feat", text2, "bare word \"Feat\" (no period) must not be touched")

    def test_already_spelled_out_featuring_not_doubled(self):
        track = self.make_track(title="Song featuring Someone")
        text = build_dedication_intro_text(track, "Justin", "")
        self.assertIn("Song featuring Someone", text)
        self.assertNotIn("featuringing", text)


# ---------------------------------------------------------------------
# 2, 3, 4, 20. Synthesis: winner-selection, advisory lock, CAS cleanup,
# already-played exclusion
# ---------------------------------------------------------------------
class DedicationSynthesisTests(DedicationFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self._dedi_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dedi_tmpdir.cleanup)
        root_patcher = patch.object(services_module, "DEDICATION_ROOT", Path(self._dedi_tmpdir.name))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

        # A selected Dedication TTS Voice -- required since r0029
        # retired the direct-Kokoro fallback this fixture used to rely
        # on when dedication_tts_voice was left blank (see
        # DedicationSharedTTSRoutingTests below for the "still blank"
        # failure-mode coverage). Winner-selection/advisory-lock/CAS-
        # cleanup/already-played-exclusion below are about the request
        # pipeline, not voice routing, so a plain always-succeeding
        # fake is enough here.
        self.dedication_voice = StationTTSVoice.objects.create(
            name="Dedication_Dave", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="am_fenrir", language="en-us", speed=1.0,
        )
        self.cfg.dedication_tts_voice = self.dedication_voice
        self.cfg.save(update_fields=["dedication_tts_voice"])

        def fake_station_synth(text, *, voice, output_path, speed=None, language=None,
                                timeout_seconds=None, service=None):
            Path(output_path).write_bytes(b"WAV")
            return Path(output_path)

        station_patcher = patch.object(services_module, "synthesize_station_voice", side_effect=fake_station_synth)
        station_patcher.start()
        self.addCleanup(station_patcher.stop)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"FLAC")
            return MagicMock(returncode=0)

        run_patcher = patch.object(services_module.subprocess, "run", side_effect=fake_run)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        duration_patcher = patch.object(services_module, "_probe_duration", return_value=6.5)
        duration_patcher.start()
        self.addCleanup(duration_patcher.stop)

        # Default no-op for the waveform-generation step -- fake_run
        # above also intercepts analyze_tracks.py's OWN ffmpeg decode
        # calls (same "ffmpeg" argv[0]), which would otherwise choke on
        # garbage mock PCM data and litter the cwd with a stray file
        # named "-" (argv[-1] for a decode-to-stdout command) every run.
        # The two tests specifically about this step override this with
        # their own `with patch(...)` block, which correctly shadows it.
        analyze_patcher = patch("library.management.commands.analyze_tracks.analyze_one_track", return_value=True)
        analyze_patcher.start()
        self.addCleanup(analyze_patcher.stop)

    def test_synthesis_attaches_intro_track_with_song_metadata(self):
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 5, 1), 5)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, requester_name="Justin")

        synthesize_dedication_intro(req)

        req.refresh_from_db()
        self.assertIsNotNone(req.intro_track_id)
        self.assertEqual(req.intro_track.title, track.title)
        self.assertEqual(req.intro_track.artist_id, track.artist_id)
        self.assertEqual(req.intro_track.category_id, self.dedication_category.id)
        self.assertEqual(req.intro_track.next_start_seconds, req.intro_track.duration_seconds)

    def test_synthesis_generates_waveform_and_reasserts_cue_points(self):
        """next_start_seconds is pre-set specifically to opt this Track
        OUT of isadoraair-analyze.timer's periodic sweep (that sweep's
        envelope-threshold detection is wrong for a few seconds of
        speech) -- but that also means the sweep would never generate a
        waveform for it either. synthesize_dedication_intro calls
        analyze_one_track directly instead, then re-asserts the
        deliberate cue points regardless of whatever it guessed."""
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 5, 1), 12)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        def fake_analyze_one_track(row, cfg_values, wave_dir, force):
            track_id = row[0]
            # Simulate what the real envelope detector would wrongly
            # guess for speech -- something short of the full duration.
            Track.objects.filter(id=track_id).update(
                waveform_path=f"/srv/isadoraair/waveforms/{track_id}.json",
                next_start_seconds=1.0, cue_in_seconds=0.5,
            )
            return True

        with patch(
            "library.management.commands.analyze_tracks.analyze_one_track",
            side_effect=fake_analyze_one_track,
        ) as mock_analyze:
            synthesize_dedication_intro(req)

        mock_analyze.assert_called_once()
        called_row = mock_analyze.call_args[0][0]
        req.refresh_from_db()
        self.assertEqual(called_row[0], req.intro_track_id)

        req.intro_track.refresh_from_db()
        self.assertTrue(req.intro_track.waveform_path, "waveform_path should now be set")
        self.assertEqual(req.intro_track.next_start_seconds, req.intro_track.duration_seconds,
                          "must be re-asserted to the full clip length, not the envelope detector's guess")
        self.assertEqual(req.intro_track.cue_in_seconds, 0,
                          "must be re-asserted to 0, not the envelope detector's guess")

    def test_waveform_generation_failure_does_not_fail_synthesis(self):
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 5, 1), 13)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        with patch(
            "library.management.commands.analyze_tracks.analyze_one_track",
            side_effect=RuntimeError("ffmpeg exploded"),
        ):
            result = synthesize_dedication_intro(req)

        self.assertTrue(result, "the intro attachment succeeded -- a cosmetic waveform failure must not undo that")
        req.refresh_from_db()
        self.assertIsNotNone(req.intro_track_id)
        self.assertEqual(req.intro_track.next_start_seconds, req.intro_track.duration_seconds)

    def test_winner_selection_survives_across_two_command_runs(self):
        """Round 4's bug: filtering intro_track__isnull=True FIRST let
        the winner's own row drop out of the query the moment it was
        synthesized, making a collapsed duplicate look like "the first
        row" on the very next cycle and get a redundant intro."""
        track = self.make_track(title="Shared Song")
        log = self.make_log(date(2027, 5, 1), 6)
        item = self.make_item(log, 0, track=track)
        req1 = self.make_request(track, status="scheduled", log_item=item,
                                  submitted_at=timezone.now() - timedelta(minutes=2))
        req2 = self.make_request(track, status="scheduled", log_item=item,
                                  submitted_at=timezone.now() - timedelta(minutes=1))

        call_command("generate_dedication_intros", stdout=StringIO())
        call_command("generate_dedication_intros", stdout=StringIO())

        self.assertEqual(Track.objects.filter(category=self.dedication_category).count(), 1)
        req1.refresh_from_db()
        req2.refresh_from_db()
        self.assertIsNotNone(req1.intro_track_id, "earliest-submitted collapsed request should win")
        self.assertIsNone(req2.intro_track_id)

    def test_already_played_log_item_excluded_from_synthesis(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 1), 7)
        item = self.make_item(log, 0, track=track, played_at=timezone.now())
        req = self.make_request(track, status="scheduled", log_item=item)

        call_command("generate_dedication_intros", stdout=StringIO())

        req.refresh_from_db()
        self.assertIsNone(req.intro_track_id)

    def test_advisory_lock_blocks_overlapping_invocation(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 1), 8)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        holder_ready = threading.Event()
        release_event = threading.Event()

        def hold_lock():
            close_old_connections()
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s, %s)", DEDICATION_LOCK_KEY)
            holder_ready.set()
            release_event.wait(timeout=5)
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", DEDICATION_LOCK_KEY)
            close_old_connections()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        holder_ready.wait(timeout=5)
        try:
            out = StringIO()
            call_command("generate_dedication_intros", stdout=out)
            self.assertIn("Another instance", out.getvalue())
        finally:
            release_event.set()
            holder.join(timeout=10)

        req.refresh_from_db()
        self.assertIsNone(req.intro_track_id)

    def test_cas_no_reference_deletes_orphaned_track(self):
        track = self.make_track()
        req = self.make_request(track, status="pending")  # not "scheduled" -- the CAS filter can't match

        synthesize_dedication_intro(req)

        req.refresh_from_db()
        self.assertIsNone(req.intro_track_id)
        self.assertEqual(Track.objects.filter(category=self.dedication_category).count(), 0)

    def test_cas_log_item_reference_preserves_track(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 1), 9)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        synthesize_dedication_intro(req)
        req.refresh_from_db()
        intro_track_id = req.intro_track_id
        self.assertIsNotNone(intro_track_id)

        # Simulate the intro having already been spliced live (its own
        # LogItem), then re-run synthesis for the SAME request --
        # deterministic path means update_or_create resolves to the
        # SAME Track row, but the CAS update itself can't match
        # (intro_track is already set, so intro_track__isnull=True
        # excludes it) -- updated == 0. The Track must be preserved
        # anyway: a LogItem still references it.
        self.make_dedication_item(log, 1, req.intro_track)
        synthesize_dedication_intro(req)

        self.assertTrue(Track.objects.filter(id=intro_track_id).exists())


# ---------------------------------------------------------------------
# Dedication TTS cutover -- synthesis-routing decision only.
# WebRequestConfig.dedication_tts_voice is null by fixture default
# (WebRequestFixtureMixin.setUp); as of r0029 that's an invalid
# configuration state (the direct-Kokoro fallback this used to run
# was retired -- see test_dedication_voice_blank_raises_configuration_
# error_not_legacy_dispatch below). DedicationSynthesisTests above
# selects a Dedication TTS Voice in its own fixture and proves the
# request pipeline (winner-selection, locking, cleanup) works through
# shared TTS. This class covers the shared-TTS routing decision itself:
# no provider-id leakage, configured timeout, non-fatal failure, the
# blank-voice failure mode, and that a successful shared synthesis
# still flows through the existing FLAC/Track pipeline.
# ---------------------------------------------------------------------
class DedicationSharedTTSRoutingTests(DedicationFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self._dedi_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dedi_tmpdir.cleanup)
        root_patcher = patch.object(services_module, "DEDICATION_ROOT", Path(self._dedi_tmpdir.name))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"FLAC")
            return MagicMock(returncode=0)

        run_patcher = patch.object(services_module.subprocess, "run", side_effect=fake_run)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        duration_patcher = patch.object(services_module, "_probe_duration", return_value=6.5)
        duration_patcher.start()
        self.addCleanup(duration_patcher.stop)

        analyze_patcher = patch("library.management.commands.analyze_tracks.analyze_one_track", return_value=True)
        analyze_patcher.start()
        self.addCleanup(analyze_patcher.stop)

    def _make_voice(self, name="Dedication_Dave", provider_voice="am_fenrir"):
        return StationTTSVoice.objects.create(
            name=name, enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice=provider_voice, language="en-us", speed=1.0,
        )

    def _select_voice(self, voice, **overrides):
        self.cfg.dedication_tts_voice = voice
        for field, value in overrides.items():
            setattr(self.cfg, field, value)
        self.cfg.save(update_fields=["dedication_tts_voice", *overrides])

    @staticmethod
    def _fake_station_synth(text, *, voice, output_path, speed=None, language=None,
                             timeout_seconds=None, service=None):
        Path(output_path).write_bytes(b"WAV")
        return Path(output_path)

    def test_dedication_voice_blank_raises_configuration_error_not_legacy_dispatch(self):
        """dedication_tts_voice is null (this fixture's/field's
        default) is an invalid configuration state as of r0029 -- the
        direct-Kokoro fallback this used to run no longer exists.
        _synthesize_dedication_wav() must raise TTSConfigurationError
        immediately, WITHOUT calling synthesize_station_voice or any
        subprocess -- and synthesize_dedication_intro()'s own outer
        try/except must absorb that non-fatally, same contract as any
        other synthesis failure (see test_shared_tts_failure_is_non_
        fatal_and_emits_warning_event below)."""
        self.assertIsNone(self.cfg.dedication_tts_voice_id, "fixture default must stay blank")
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 6, 1), 5)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, requester_name="Justin")

        from isadoraair.tts.errors import TTSConfigurationError
        with patch.object(services_module, "synthesize_station_voice") as mock_station, \
             patch.object(services_module, "emit_event") as mock_emit:
            with self.assertRaises(TTSConfigurationError):
                services_module._synthesize_dedication_wav(self.cfg, "text", Path("/unused"))
            result = synthesize_dedication_intro(req)

        self.assertFalse(result, "best-effort: a blank dedication voice must not raise or crash the caller")
        mock_station.assert_not_called()
        services_module.subprocess.run.assert_not_called()
        req.refresh_from_db()
        self.assertIsNone(req.intro_track_id, "no partial Track should be attached")
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["category"], "webrequests")
        self.assertEqual(mock_emit.call_args.kwargs["level"], "warning")

    def test_shared_tts_path_uses_logical_voice_and_never_leaks_provider_voice_id(self):
        voice = self._make_voice(name="Dedication_Dave", provider_voice="am_fenrir")
        self._select_voice(voice)
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 6, 1), 6)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, requester_name="Justin")

        with patch.object(services_module, "synthesize_station_voice",
                           side_effect=self._fake_station_synth) as mock_station:
            result = synthesize_dedication_intro(req)

        self.assertTrue(result)
        mock_station.assert_called_once()
        args, kwargs = mock_station.call_args
        self.assertEqual(kwargs["voice"], "Dedication_Dave", "caller must pass the logical name, not a provider id")
        self.assertNotIn("am_fenrir", (args, kwargs), "the Kokoro provider voice id must never reach the caller")

    def test_configured_timeout_passed_through_to_station_service(self):
        voice = self._make_voice()
        self._select_voice(voice, dedication_tts_timeout_seconds=17)
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 6, 1), 7)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        with patch.object(services_module, "synthesize_station_voice",
                           side_effect=self._fake_station_synth) as mock_station:
            synthesize_dedication_intro(req)

        self.assertEqual(mock_station.call_args.kwargs["timeout_seconds"], 17)

    def test_shared_tts_failure_is_non_fatal_and_emits_warning_event(self):
        voice = self._make_voice()
        self._select_voice(voice)
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 6, 1), 8)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item)

        with patch.object(services_module, "synthesize_station_voice",
                           side_effect=RuntimeError("provider unavailable")), \
             patch.object(services_module, "emit_event") as mock_emit:
            result = synthesize_dedication_intro(req)

        self.assertFalse(result, "best-effort: a shared-TTS failure must not raise or crash the caller")
        req.refresh_from_db()
        self.assertIsNone(req.intro_track_id, "no partial Track should be attached on failure")
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["category"], "webrequests")
        self.assertEqual(mock_emit.call_args.kwargs["level"], "warning")
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Dedication intro synthesis failed")
        self.assertEqual(
            list(Path(self._dedi_tmpdir.name).glob(".request-*")), [],
            "temp files must still be cleaned up on a shared-TTS failure",
        )

    def test_successful_shared_tts_synthesis_still_attaches_track_via_existing_pipeline(self):
        voice = self._make_voice()
        self._select_voice(voice)
        track = self.make_track(title="Free Fallin'")
        log = self.make_log(date(2027, 6, 1), 9)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, requester_name="Justin")

        with patch.object(services_module, "synthesize_station_voice", side_effect=self._fake_station_synth):
            result = synthesize_dedication_intro(req)

        self.assertTrue(result)
        req.refresh_from_db()
        self.assertIsNotNone(req.intro_track_id)
        self.assertEqual(req.intro_track.title, track.title)
        self.assertEqual(req.intro_track.artist_id, track.artist_id)
        self.assertEqual(req.intro_track.category_id, self.dedication_category.id)
        self.assertEqual(req.intro_track.duration_seconds, 6.5)
        self.assertTrue(
            Path(req.intro_track.filepath).exists(),
            "ffmpeg WAV->FLAC conversion must still run for the shared-TTS path",
        )


# ---------------------------------------------------------------------
# 5, 6, 7. Splice mechanics -- returns intro not song, slot-wide
# guard/marker, no repeated intro
# ---------------------------------------------------------------------
class DedicationSpliceTests(DedicationFixtureMixin, TransactionTestCase):
    def test_returns_intro_item_not_song(self):
        """Round 2's bug: a self-defeating cursor sequence made this
        return the SONG instead of the just-spliced INTRO."""
        track = self.make_track()
        log = self.make_log(date(2027, 5, 2), 5)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=song_item)
        req.intro_track = intro_track
        req.save(update_fields=["intro_track"])

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1  # normal walk already advanced past song_item

        result = stand_in._maybe_insert_dedication_intro(song_item)

        self.assertNotEqual(result.id, song_item.id)
        self.assertEqual(result.track_id, intro_track.id)
        self.assertEqual([i.id for i in stand_in._forced_next_items], [song_item.id])
        self.assertEqual(stand_in._queue_cursor, 1)
        self.assertEqual([i.id for i in stand_in.log_items], [result.id, song_item.id])

    def test_slot_wide_marker_covers_all_collapsed_requests(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 2), 6)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req1 = self.make_request(track, status="scheduled", log_item=song_item,
                                  submitted_at=timezone.now() - timedelta(minutes=2))
        req1.intro_track = intro_track
        req1.save(update_fields=["intro_track"])
        req2 = self.make_request(track, status="scheduled", log_item=song_item,
                                  submitted_at=timezone.now() - timedelta(minutes=1))

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1

        stand_in._maybe_insert_dedication_intro(song_item)

        req1.refresh_from_db()
        req2.refresh_from_db()
        self.assertIsNotNone(req1.intro_log_item_id)
        self.assertEqual(req1.intro_log_item_id, req2.intro_log_item_id,
                          "the marker must cover EVERY collapsed request sharing this log_item, not just the winner")

    def test_no_second_intro_after_song_returns_via_forced_list(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 2), 7)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=song_item)
        req.intro_track = intro_track
        req.save(update_fields=["intro_track"])

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1
        first = stand_in._maybe_insert_dedication_intro(song_item)
        self.assertNotEqual(first.id, song_item.id)

        second = stand_in._maybe_insert_dedication_intro(song_item)
        self.assertEqual(second.id, song_item.id,
                          "already_spliced guard must return the song plainly, not splice a second intro")


# ---------------------------------------------------------------------
# 10, 24. Splice transactionality -- a forced zero-row claim rolls back
# the position shift AND the intro LogItem together
# ---------------------------------------------------------------------
class DedicationTransactionalityTests(DedicationFixtureMixin, TransactionTestCase):
    def test_zero_row_claim_rolls_back_cleanly(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 4), 10)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=song_item)
        req.intro_track = intro_track
        req.save(update_fields=["intro_track"])

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1

        class _ZeroUpdateQuerySet:
            def update(self, **kwargs):
                return 0

        real_manager_filter = SongRequest.objects.filter

        def fake_manager_filter(*args, **kwargs):
            # Only the slot-wide claim query is called directly on the
            # Manager with exactly this kwarg shape -- the earlier
            # select_for_update() lookup chains .filter() off the
            # QuerySet select_for_update() returns, a different call
            # site entirely, left untouched.
            if set(kwargs) == {"status", "log_item_id", "track_id"}:
                return _ZeroUpdateQuerySet()
            return real_manager_filter(*args, **kwargs)

        with patch.object(SongRequest.objects, "filter", side_effect=fake_manager_filter):
            with self.assertRaises(RuntimeError):
                stand_in._maybe_insert_dedication_intro_inner(song_item)

        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), 1,
                          "the just-created intro LogItem must be rolled back, not committed")
        self.assertEqual([i.id for i in stand_in.log_items], [song_item.id],
                          "in-memory state must be untouched -- the post-transaction apply step never ran")
        req.refresh_from_db()
        self.assertIsNone(req.intro_log_item_id)

    def test_fail_open_wrapper_recovers_from_the_same_failure(self):
        """The raw _inner failure above is real -- but the outer
        _maybe_insert_dedication_intro wrapper (what production actually
        calls) must turn it into a plain-song fallback, never a
        propagated exception."""
        track = self.make_track()
        log = self.make_log(date(2027, 5, 4), 11)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=song_item)
        req.intro_track = intro_track
        req.save(update_fields=["intro_track"])

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1

        with patch.object(eng_module.PlaybackEngine, "_maybe_insert_dedication_intro_inner",
                           side_effect=RuntimeError("no claim")):
            result = stand_in._maybe_insert_dedication_intro(song_item)

        self.assertIs(result, song_item)


# ---------------------------------------------------------------------
# 8. Rollover during intro -- preview/peek/reconciliation all agree the
# request is still "scheduled"
# ---------------------------------------------------------------------
class DedicationRolloverTests(DedicationFixtureMixin, TransactionTestCase):
    def test_rollover_preview_peek_and_reconciliation_agree_still_scheduled(self):
        old_log = self.make_log(date(2027, 5, 3), 5)
        track = self.make_track()
        song_item = self.make_item(old_log, 1, track=track, scheduled_time=timezone.now() + timedelta(seconds=5))
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(old_log, 0, intro_track, scheduled_time=timezone.now())
        req = self.make_request(track, status="scheduled", log_item=song_item, scheduled_at=timezone.now(),
                                 intro_track=intro_track, intro_log_item=intro_item)

        stand_in = make_engine_stand_in()
        stand_in.current_log = None  # rollover already emptied the old hour
        stand_in.log_items = []
        stand_in._queue_cursor = 0
        stand_in._forced_next_items = [song_item]

        peeked = eng_module.PlaybackEngine._peek_playable_at_cursor(stand_in)
        self.assertEqual(peeked.id, song_item.id)

        preview = eng_module.PlaybackEngine._get_upcoming_preview(stand_in)
        self.assertIn(song_item.id, [it.id for it in preview])

        self.write_state(
            decks={"A": None, "B": None}, queue=[{"item_id": song_item.id}],
            date_str="2027-05-03", hour=6,
        )
        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(req.log_item_id, song_item.id)


# ---------------------------------------------------------------------
# 9, 19, 21. _start_next_track call site -- forced items bypass both
# hooks, fail-open containment, best-effort last-second scheduling
# ---------------------------------------------------------------------
class DedicationStartNextTrackTests(DedicationFixtureMixin, TransactionTestCase):
    def test_forced_item_bypasses_scheduling_and_dedication_hooks(self):
        stand_in = make_engine_stand_in()
        forced_item = MagicMock(id=1, category_id=5)
        forced_item.category.code = "Music"
        stand_in._next_queue_item = MagicMock(return_value=(forced_item, True))
        stand_in._create_deck = MagicMock(return_value=MagicMock())
        stand_in._restore_followup_for_intro = MagicMock()

        with patch.object(eng_module, "maybe_schedule_song_request") as mock_sched, \
             patch.object(eng_module.PlaybackEngine, "_maybe_insert_dedication_intro") as mock_dedi:
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        mock_sched.assert_not_called()
        mock_dedi.assert_not_called()
        stand_in._restore_followup_for_intro.assert_not_called()
        stand_in._create_deck.assert_called_once_with("A", forced_item)

    def test_scheduling_contended_never_invoked_for_forced_items(self):
        stand_in = make_engine_stand_in()
        forced_song = MagicMock(id=2, category_id=5)
        forced_song.category.code = "Music"
        stand_in._next_queue_item = MagicMock(return_value=(forced_song, True))
        stand_in._create_deck = MagicMock(return_value=MagicMock())

        with patch.object(eng_module, "maybe_schedule_song_request",
                           return_value=eng_module.SCHEDULING_CONTENDED) as mock_sched:
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        mock_sched.assert_not_called()
        stand_in._create_deck.assert_called_once_with("A", forced_song)

    def test_unexpected_dedication_exception_falls_back_to_plain_song(self):
        stand_in = make_engine_stand_in()
        song_item = MagicMock(id=11, category_id=1)
        song_item.category.code = "Music"
        stand_in._next_queue_item = MagicMock(return_value=(song_item, False))
        stand_in._create_deck = MagicMock(return_value=MagicMock())

        with patch.object(eng_module, "maybe_schedule_song_request", side_effect=lambda li: li), \
             patch.object(eng_module.PlaybackEngine, "_maybe_insert_dedication_intro_inner",
                           side_effect=ValueError("boom")):
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._create_deck.assert_called_once_with("A", song_item)

    def test_last_second_scheduled_request_with_no_intro_yet_plays_plainly(self):
        """Real, unmocked maybe_schedule_song_request + real,
        unmocked _maybe_insert_dedication_intro -- proves the
        best-effort policy end to end: a request scheduled by the
        engine's own last-second safety net, zero lead time, no
        intro_track yet, plays with no intro and no error."""
        log = self.make_log(date(2027, 5, 9), 6)
        rotation_track = self.make_track(title="Rotation")
        song_item = self.make_item(log, 0, track=rotation_track)
        requested_track = self.make_track(title="Just Requested")
        self.make_request(requested_track, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1
        stand_in._next_queue_item = MagicMock(return_value=(song_item, False))
        stand_in._create_deck = MagicMock(return_value=MagicMock())

        eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._create_deck.assert_called_once()
        called_item = stand_in._create_deck.call_args[0][1]
        self.assertEqual(called_item.track_id, requested_track.id, "last-second scheduling swapped the track in")


# ---------------------------------------------------------------------
# 11. PlayEvent exclusion for Dedications plays (static-source check --
# _create_deck needs a real GStreamer pipeline to exercise live, same
# reasoning as the scheduling suite's played_at_written check)
# ---------------------------------------------------------------------
class DedicationPlayEventTests(TransactionTestCase):
    def test_create_deck_excludes_playevent_for_dedications_category(self):
        src = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        self.assertIn('log_item.category.code == "Dedications"', src)
        self.assertIn("is_dedication_play", src)
        self.assertIn("if not is_dedication_play:", src)
        self.assertIn("PlayEvent.objects.create(", src)


# ---------------------------------------------------------------------
# 12. Lifecycle: abandonment clears intro_log_item; fulfilled retains
# it; intro_track is never cleared
# ---------------------------------------------------------------------
class DedicationLifecycleTests(DedicationFixtureMixin, TransactionTestCase):
    def test_refresh_clears_intro_log_item_on_stranded_requeue(self):
        old_log = self.make_log(date(2027, 5, 5), 5)
        track = self.make_track()
        item = self.make_item(old_log, 0, scheduled_time=timezone.now() + timedelta(seconds=5), track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now(),
                                 intro_track=intro_track, intro_log_item=item)
        self.write_state(date_str="2027-05-05", hour=6)  # active hour moved past hour 5

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)
        self.assertIsNone(req.intro_log_item_id)
        self.assertIsNotNone(req.intro_track_id, "intro_track must survive -- reusable if rescheduled")

    def test_refresh_clears_intro_log_item_on_unavailable(self):
        track = self.make_track(ready2air=False)
        log = self.make_log(date(2027, 5, 5), 7)
        item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now(),
                                 intro_track=intro_track, intro_log_item=item)

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "unavailable")
        self.assertIsNone(req.intro_log_item_id)
        self.assertIsNotNone(req.intro_track_id)

    def test_self_heal_promotion_to_fulfilled_retains_intro_log_item(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 5), 8)
        item = self.make_item(log, 0, track=track, played_at=timezone.now())
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now(),
                                 intro_track=intro_track, intro_log_item=item)

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")
        self.assertEqual(req.intro_log_item_id, item.id, "fulfilled retains the marker as historical evidence")
        self.assertIsNotNone(req.intro_track_id)

    def test_admin_save_model_clears_intro_log_item_on_regression_to_pending(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 5), 9)
        item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now(),
                                 intro_track=intro_track, intro_log_item=item)

        admin_instance = SongRequestAdmin(SongRequest, admin.site)
        req.status = "pending"
        admin_instance.save_model(request=None, obj=req, form=None, change=True)

        req.refresh_from_db()
        self.assertIsNone(req.intro_log_item_id)
        self.assertIsNotNone(req.intro_track_id)

    def test_admin_save_model_retains_intro_log_item_on_fulfilled(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 5), 10)
        item = self.make_item(log, 0, track=track, played_at=timezone.now())
        intro_track = self.make_dedication_track()
        req = self.make_request(track, status="scheduled", log_item=item,
                                 intro_track=intro_track, intro_log_item=item)

        admin_instance = SongRequestAdmin(SongRequest, admin.site)
        req.status = "fulfilled"
        req.fulfilled_at = timezone.now()
        req.resolved_at = timezone.now()
        admin_instance.save_model(request=None, obj=req, form=None, change=True)

        req.refresh_from_db()
        self.assertEqual(req.intro_log_item_id, item.id)


# ---------------------------------------------------------------------
# 13, 17, 23. _insert_urgent_next -- unchanged behavior, forced-aware
# priority, shared bounded lock, lookup-failure retry
# ---------------------------------------------------------------------
class DedicationUrgentInsertTests(DedicationFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        spot_kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        self.alert_category = Category.objects.create(code="TESTALERT", name="Test Alert", kind=spot_kind)

    def _make_alert_track(self):
        track = self.make_track(title="Weather Alert")
        track.category = self.alert_category
        track.save(update_fields=["category"])
        return track

    def test_insert_urgent_next_unchanged_behavior_without_forced_items(self):
        log = self.make_log(date(2027, 5, 6), 5)
        song_item = self.make_item(log, 0, track=self.make_track(title="Rotation"))
        alert_track = self._make_alert_track()

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 0

        stand_in._insert_urgent_next_inner("TESTALERT")

        self.assertEqual(len(stand_in.log_items), 2)
        self.assertEqual(stand_in.log_items[0].track_id, alert_track.id)
        self.assertEqual(stand_in._forced_next_items, [], "no forced items pending -- plain insert-at-cursor, unchanged")
        self.assertEqual(stand_in._urgent_retry_counts.get("TESTALERT"), 0)

    def test_insert_urgent_next_prepends_ahead_of_pending_dedication_when_forced_active(self):
        log = self.make_log(date(2027, 5, 6), 6)
        song_item = self.make_item(log, 0, track=self.make_track(title="Rotation"))
        dedication_followup = self.make_item(log, 1, track=self.make_track(title="Dedicated Song"))
        alert_track = self._make_alert_track()

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item, dedication_followup])
        stand_in._queue_cursor = 1
        stand_in._forced_next_items = [dedication_followup]

        stand_in._insert_urgent_next_inner("TESTALERT")

        self.assertEqual(stand_in._forced_next_items[0].track_id, alert_track.id,
                          "urgent alert jumps ahead of a pending dedication follow-up -- safety first")
        self.assertEqual(stand_in._forced_next_items[1].id, dedication_followup.id)

    def test_urgent_and_dedication_paths_share_the_same_bounded_lock(self):
        src_urgent = inspect.getsource(eng_module.PlaybackEngine._insert_urgent_next_inner)
        src_dedi = inspect.getsource(eng_module.PlaybackEngine._maybe_insert_dedication_intro_inner)
        self.assertIn("_locked_playlist_log", src_urgent)
        self.assertIn("_locked_playlist_log", src_dedi)

    def test_lock_contention_retries_without_raising_or_blocking(self):
        log = self.make_log(date(2027, 5, 6), 7)
        self._make_alert_track()
        stand_in = self.make_engine_stand_in(current_log=log, log_items=[self.make_item(log, 0, track=self.make_track())])

        holder_ready = threading.Event()
        release_event = threading.Event()

        def hold_lock():
            close_old_connections()
            with transaction.atomic():
                PlaylistLog.objects.select_for_update().get(pk=log.pk)
                holder_ready.set()
                release_event.wait(timeout=5)
            close_old_connections()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        holder_ready.wait(timeout=5)
        try:
            with patch.object(eng_module.GLib, "timeout_add_seconds") as mock_timeout:
                stand_in._insert_urgent_next("TESTALERT")
        finally:
            release_event.set()
            holder.join(timeout=10)

        mock_timeout.assert_called_once()
        self.assertEqual(stand_in._urgent_retry_counts.get("TESTALERT"), 1)

    def test_urgent_lookup_failure_schedules_retry_not_lost(self):
        stand_in = self.make_engine_stand_in(current_log=MagicMock(), log_items=[MagicMock()])
        with patch.object(eng_module.Category.objects, "get", side_effect=RuntimeError("db down")):
            with patch.object(eng_module.GLib, "timeout_add_seconds") as mock_timeout:
                stand_in._insert_urgent_next("TESTALERT")
        mock_timeout.assert_called_once()
        self.assertEqual(stand_in._urgent_retry_counts.get("TESTALERT"), 1)

    def test_repeated_contention_emits_high_severity_event_and_resets(self):
        stand_in = self.make_engine_stand_in(current_log=MagicMock(), log_items=[MagicMock()])
        stand_in._urgent_retry_counts["TESTALERT"] = 5
        with patch.object(eng_module.Category.objects, "get", side_effect=RuntimeError("db down")):
            with patch.object(eng_module, "emit_event") as mock_emit, \
                 patch.object(eng_module.GLib, "timeout_add_seconds") as mock_timeout:
                stand_in._insert_urgent_next("TESTALERT")
        mock_emit.assert_called_once()
        mock_timeout.assert_not_called()
        self.assertEqual(stand_in._urgent_retry_counts.get("TESTALERT"), 0)


# ---------------------------------------------------------------------
# 17 (dedication half). Dedication-side lock contention plays plainly
# ---------------------------------------------------------------------
class DedicationLockContentionTests(DedicationFixtureMixin, TransactionTestCase):
    def test_dedication_lock_contention_plays_song_plainly_no_exception(self):
        track = self.make_track()
        log = self.make_log(date(2027, 5, 9), 5)
        song_item = self.make_item(log, 0, track=track)
        intro_track = self.make_dedication_track()
        self.make_request(track, status="scheduled", log_item=song_item, intro_track=intro_track)

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[song_item])
        stand_in._queue_cursor = 1

        class _FakeCause(Exception):
            pgcode = "55P03"

        contended = OperationalError("lock timeout")
        contended.__cause__ = _FakeCause()

        with patch.object(eng_module.PlaybackEngine, "_locked_playlist_log", side_effect=contended):
            result = stand_in._maybe_insert_dedication_intro(song_item)

        self.assertIs(result, song_item)


# ---------------------------------------------------------------------
# 14. Resume-hint selection -- Dedications deck beats an older/longer
# outgoing music deck, and clears the 5s position floor
# ---------------------------------------------------------------------
class DedicationResumeHintTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self._state_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_tmpdir.cleanup)
        self.engine_state_path = Path(self._state_tmpdir.name) / "engine_state.json"
        patcher = patch.object(eng_module, "STATE_PATH", self.engine_state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_engine_state(self, decks, age_seconds=1.0):
        self.engine_state_path.write_text(json.dumps({"decks": decks}), encoding="utf-8")
        mtime = time.time() - age_seconds
        os.utime(self.engine_state_path, (mtime, mtime))

    def test_dedication_deck_wins_over_older_longer_music_deck(self):
        self._write_engine_state({
            "A": {"track_id": 101, "log_item_id": 5000, "position": 180.0, "category": "Music"},
            "B": {"track_id": 202, "log_item_id": 5001, "position": 2.0, "category": "Dedications"},
        })
        stand_in = make_engine_stand_in()

        eng_module.PlaybackEngine._read_resume_hint(stand_in)

        self.assertIsNotNone(stand_in._resume_hint)
        self.assertEqual(stand_in._resume_hint["track_id"], 202)
        self.assertEqual(stand_in._resume_hint["log_item_id"], 5001)

    def test_dedication_deck_below_five_seconds_still_considered(self):
        self._write_engine_state({
            "A": None,
            "B": {"track_id": 303, "log_item_id": 6000, "position": 1.2, "category": "Dedications"},
        })
        stand_in = make_engine_stand_in()

        eng_module.PlaybackEngine._read_resume_hint(stand_in)

        self.assertIsNotNone(stand_in._resume_hint)
        self.assertEqual(stand_in._resume_hint["track_id"], 303)

    def test_non_dedication_deck_under_five_seconds_still_excluded(self):
        self._write_engine_state({
            "A": {"track_id": 404, "log_item_id": 7000, "position": 3.0, "category": "Music"},
            "B": None,
        })
        stand_in = make_engine_stand_in()

        eng_module.PlaybackEngine._read_resume_hint(stand_in)

        self.assertIsNone(stand_in._resume_hint)


# ---------------------------------------------------------------------
# 15. Forced-only startup gate (static check -- start() drives real
# GStreamer pipeline construction, not practical to run live here)
# ---------------------------------------------------------------------
class DedicationStartupGateTests(TransactionTestCase):
    def test_start_gate_considers_forced_items(self):
        src = inspect.getsource(eng_module.PlaybackEngine.start)
        self.assertIn("_restore_dedication_sequence_from_resume_hint", src)
        self.assertIn("if not self.log_items and not self._forced_next_items:", src)


# ---------------------------------------------------------------------
# 16. VoiceTrack precedence -- dedication supersedes incoming intro VT;
# outgoing outro VT still fires first
# ---------------------------------------------------------------------
class DedicationVoiceTrackTests(DedicationFixtureMixin, TransactionTestCase):
    def test_incoming_vt_suppressed_when_dedication_pending(self):
        log = self.make_log(date(2027, 5, 7), 5)
        song_track = self.make_track(title="Song")
        song_item = self.make_item(log, 0, track=song_track)
        intro_track = self.make_dedication_track()
        self.make_request(song_track, status="scheduled", log_item=song_item, intro_track=intro_track)

        vt_path = Path(self._tmpdir.name) / "intro-vt.mp3"
        vt_path.touch()
        VoiceTrack.objects.create(track=song_track, position="intro", filepath=str(vt_path), gain_db=0.0)

        stand_in = make_engine_stand_in()
        stand_in._peek_playable_at_cursor = MagicMock(return_value=song_item)
        outgoing_deck = MagicMock()
        outgoing_deck.track = self.make_track(title="Outgoing")  # no outro VT configured

        result = eng_module.PlaybackEngine._vt_maybe_enter(stand_in, outgoing_deck)

        self.assertFalse(result, "no VTs remain once the incoming intro VT is suppressed by a pending dedication")

    def test_outgoing_vt_still_fires_first_despite_pending_dedication(self):
        log = self.make_log(date(2027, 5, 7), 6)
        song_track = self.make_track(title="Song2")
        song_item = self.make_item(log, 0, track=song_track)
        intro_track = self.make_dedication_track()
        self.make_request(song_track, status="scheduled", log_item=song_item, intro_track=intro_track)

        outgoing_track = self.make_track(title="Outgoing2")
        outgoing_vt_path = Path(self._tmpdir.name) / "outro-vt.mp3"
        outgoing_vt_path.touch()
        VoiceTrack.objects.create(track=outgoing_track, position="outro", filepath=str(outgoing_vt_path), gain_db=0.0)

        stand_in = make_engine_stand_in()
        stand_in._vt_lock = threading.RLock()
        stand_in._peek_playable_at_cursor = MagicMock(return_value=song_item)
        stand_in._vt_ramp_duck_to_db = MagicMock()
        stand_in._vt_fire_file = MagicMock(return_value="fire-1")
        outgoing_deck = MagicMock()
        outgoing_deck.track = outgoing_track

        result = eng_module.PlaybackEngine._vt_maybe_enter(stand_in, outgoing_deck)

        self.assertTrue(result)
        self.assertIsNone(stand_in._vt["incoming_vt_filepath"], "incoming intro VT suppressed by the pending dedication")
        stand_in._vt_fire_file.assert_called_once()
        self.assertEqual(stand_in._vt["phase"], "outro_playing")


# ---------------------------------------------------------------------
# 18, 22, 25. Restart recovery -- resume-hint reconstruction across an
# hour boundary, ordinary-cursor-walk restoration surviving a
# subsequent rollover, and fail-safe behavior when either lookup fails
# ---------------------------------------------------------------------
class DedicationRestartRecoveryTests(DedicationFixtureMixin, TransactionTestCase):
    def test_resume_hint_restores_both_intro_and_song_across_hour_boundary(self):
        old_log = self.make_log(date(2027, 5, 8), 5)  # wall clock has since moved to hour 6
        track = self.make_track()
        song_item = self.make_item(old_log, 1, track=track)
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(old_log, 0, intro_track)
        self.make_request(track, status="scheduled", log_item=song_item,
                           intro_track=intro_track, intro_log_item=intro_item)

        stand_in = make_engine_stand_in()
        stand_in._resume_hint = {"track_id": intro_track.id, "position": 2.0, "log_item_id": intro_item.id}

        eng_module.PlaybackEngine._restore_dedication_sequence_from_resume_hint(stand_in)

        self.assertEqual([i.id for i in stand_in._forced_next_items], [intro_item.id, song_item.id])

    def test_resume_hint_restore_noop_when_no_hint(self):
        stand_in = make_engine_stand_in()
        stand_in._resume_hint = None
        eng_module.PlaybackEngine._restore_dedication_sequence_from_resume_hint(stand_in)
        self.assertEqual(stand_in._forced_next_items, [])

    def test_resume_hint_restore_noop_when_no_matching_request(self):
        stand_in = make_engine_stand_in()
        stand_in._resume_hint = {"track_id": 999, "position": 2.0, "log_item_id": 424242}
        eng_module.PlaybackEngine._restore_dedication_sequence_from_resume_hint(stand_in)
        self.assertEqual(stand_in._forced_next_items, [])

    def test_resume_hint_restore_never_raises_on_query_failure(self):
        stand_in = make_engine_stand_in()
        stand_in._resume_hint = {"track_id": 1, "position": 2.0, "log_item_id": 555}
        with patch.object(eng_module.SongRequest.objects, "filter", side_effect=RuntimeError("db down")):
            eng_module.PlaybackEngine._restore_dedication_sequence_from_resume_hint(stand_in)  # must not raise
        self.assertEqual(stand_in._forced_next_items, [])

    def test_ordinary_cursor_walk_restoration_survives_subsequent_rollover(self):
        """Covers the round-7 regression: a crash after the splice
        commits but before the intro's own deck is ever created has no
        relevant resume hint at all -- the intro is reached purely
        through the ordinary (non-forced) cursor walk on restart."""
        log = self.make_log(date(2027, 5, 8), 7)
        track = self.make_track()
        song_item = self.make_item(log, 1, track=track)
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(log, 0, intro_track)
        self.make_request(track, status="scheduled", log_item=song_item,
                           intro_track=intro_track, intro_log_item=intro_item)

        stand_in = self.make_engine_stand_in(current_log=log, log_items=[intro_item, song_item])
        stand_in._queue_cursor = 0
        stand_in._resume_hint = None

        fetched_intro, is_forced = eng_module.PlaybackEngine._next_queue_item(stand_in)
        self.assertEqual(fetched_intro.id, intro_item.id)
        self.assertFalse(is_forced)

        restored = eng_module.PlaybackEngine._restore_followup_for_intro(stand_in, fetched_intro)
        self.assertTrue(restored)
        self.assertEqual([i.id for i in stand_in._forced_next_items], [song_item.id])

        # Rollover to an entirely new hour -- the forced song must
        # still come back next regardless.
        new_log = self.make_log(date(2027, 5, 8), 8)
        stand_in.current_log = new_log
        stand_in.log_items = []
        stand_in._queue_cursor = 0

        next_item, is_forced2 = eng_module.PlaybackEngine._next_queue_item(stand_in)
        self.assertEqual(next_item.id, song_item.id)
        self.assertTrue(is_forced2)

    def test_restore_followup_returns_false_on_query_exception(self):
        stand_in = make_engine_stand_in()
        fake_intro_item = MagicMock(id=999)

        with patch.object(eng_module.SongRequest.objects, "filter", side_effect=RuntimeError("db down")):
            result = eng_module.PlaybackEngine._restore_followup_for_intro(stand_in, fake_intro_item)

        self.assertFalse(result)
        self.assertEqual(stand_in._forced_next_items, [])

    def test_restore_followup_returns_false_when_no_matching_request(self):
        log = self.make_log(date(2027, 5, 8), 9)
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(log, 0, intro_track)
        # No SongRequest references this intro_item at all.

        stand_in = make_engine_stand_in()
        result = eng_module.PlaybackEngine._restore_followup_for_intro(stand_in, intro_item)

        self.assertFalse(result)

    def test_restore_followup_returns_false_when_song_file_missing(self):
        """Regression: confirming the SongRequest row exists isn't
        enough -- the paired song must actually be playable, or an
        intro could air with no playable song behind it (the row
        exists, is correctly paired, but the Track's file is gone from
        disk)."""
        log = self.make_log(date(2027, 5, 8), 10)
        missing_path = str(Path(self._tmpdir.name) / "missing-song.mp3")
        track = self.make_track(filepath=missing_path)
        song_item = self.make_item(log, 1, track=track)
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(log, 0, intro_track)
        self.make_request(track, status="scheduled", log_item=song_item,
                           intro_track=intro_track, intro_log_item=intro_item)

        stand_in = make_engine_stand_in()
        result = eng_module.PlaybackEngine._restore_followup_for_intro(stand_in, intro_item)

        self.assertFalse(result, "an unplayable paired song must not protect the intro")
        self.assertEqual(stand_in._forced_next_items, [])

    def test_missing_song_file_intro_skipped_never_reaches_create_deck(self):
        """Same scenario as above, exercised through the real
        _start_next_track call site (unmocked _restore_followup_for_intro)
        -- the intro must be skipped entirely, never handed to
        _create_deck, rather than airing with no playable song behind it."""
        log = self.make_log(date(2027, 5, 8), 11)
        missing_path = str(Path(self._tmpdir.name) / "missing-song-2.mp3")
        track = self.make_track(filepath=missing_path)
        song_item = self.make_item(log, 1, track=track)
        intro_track = self.make_dedication_track()
        intro_item = self.make_dedication_item(log, 0, intro_track)
        self.make_request(track, status="scheduled", log_item=song_item,
                           intro_track=intro_track, intro_log_item=intro_item)

        stand_in = make_engine_stand_in()
        stand_in._next_queue_item = MagicMock(side_effect=[(intro_item, False), (None, False)])
        stand_in._create_deck = MagicMock(return_value=MagicMock())
        stand_in._on_log_exhausted = MagicMock()

        with patch.object(eng_module, "maybe_schedule_song_request", side_effect=lambda li: li):
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._create_deck.assert_not_called()
        stand_in._on_log_exhausted.assert_called_once_with("A")

    def test_start_next_track_skips_unpaired_intro_via_synchronous_recursion(self):
        stand_in = make_engine_stand_in()
        dedication_category = Category.objects.get(code="Dedications")
        intro_item = MagicMock(id=1, category_id=dedication_category.id)
        intro_item.category = dedication_category
        next_song_item = MagicMock(id=2, category_id=None)

        stand_in._next_queue_item = MagicMock(side_effect=[(intro_item, False), (next_song_item, False)])
        stand_in._restore_followup_for_intro = MagicMock(return_value=False)
        stand_in._create_deck = MagicMock(return_value=MagicMock())
        stand_in._on_log_exhausted = MagicMock()

        with patch.object(eng_module, "maybe_schedule_song_request", side_effect=lambda li: li):
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._restore_followup_for_intro.assert_called_once_with(intro_item)
        stand_in._create_deck.assert_called_once_with("A", next_song_item)
