"""r0123 corrective B3: bounded long-form Refresh Waveform Only (duration ceiling, per-decode byte
caps and timeouts enforced while decoding). Limits are exercised with synthetic decoders and patched
bounds -- no long media is created. Per-test temporary directories only."""
import os
import sys
import time
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client

from library.management.commands import analyze_tracks
from library.models import Track
from library.services import waveform_cache, waveform_generation as wg
from library.tests.test_waveform_integrity_package_b import _audio, _Base
from monitoring.models import SystemEvent


# ------------------------------------------------------------------ B3: bounded long-form refresh

class ResourceLimitTests(_Base):
    SETTINGS = {"sample_rate": 4410, "window_seconds": 0.05, "waveform_points": 1000}

    def test_ordinary_and_long_form_durations_are_supported_within_the_budget(self):
        for seconds in (240, 3600, 7200, wg.MAX_REFRESH_SECONDS - 60, wg.MAX_REFRESH_SECONDS):
            with self.subTest(seconds=seconds):
                limits, status, why = wg.refresh_limits(seconds, self.SETTINGS)
                self.assertIsNotNone(limits, why)
                expected = (seconds * wg.DURATION_SLACK_RATIO + wg.DURATION_SLACK_SECONDS) * 4410 * 4
                self.assertAlmostEqual(limits.max_stereo_bytes, min(int(expected), wg.PCM_BUDGET_BYTES), delta=1)
                self.assertLessEqual(limits.max_stereo_bytes, wg.PCM_BUDGET_BYTES)
                self.assertAlmostEqual(limits.timeout, wg.decode_timeout(
                    seconds * wg.DURATION_SLACK_RATIO + wg.DURATION_SLACK_SECONDS), places=3)
        self.assertEqual(wg.refresh_limits(wg.MAX_REFRESH_SECONDS + 1, self.SETTINGS)[1], wg.TOO_LONG)
        self.assertEqual(wg.refresh_limits(7200, {**self.SETTINGS, "sample_rate": 22050})[1], wg.RESOURCE_LIMIT)
        unknown, _, _ = wg.refresh_limits(None, self.SETTINGS)                 # no recorded duration
        self.assertLessEqual(unknown.max_stereo_bytes, wg.PCM_BUDGET_BYTES)

    def test_a_track_beyond_the_maximum_is_refused_without_decoding(self):
        track = self.track()
        old, row = self.cache(track).read_bytes(), self.full_row(track)
        Track.objects.filter(pk=track.pk).update(duration_seconds=wg.MAX_REFRESH_SECONDS + 600)
        with patch.object(wg, "measure", side_effect=AssertionError("decoded")):
            self.assertEqual(wg.refresh(track.pk).status, wg.TOO_LONG)
            with patch.object(wg.subprocess, "Popen", side_effect=AssertionError("spawned")):
                self.assertEqual(wg.start_background(track.pk).status, wg.TOO_LONG)
        client = Client()
        client.force_login(self.user)
        response = client.post(f"/api/tracks/{track.pk}/refresh-waveform/")
        self.assertEqual((response.status_code, response.json()["status"]), (422, "too_long"))
        self.assertEqual(self.cache(track).read_bytes(), old)
        Track.objects.filter(pk=track.pk).update(duration_seconds=row["duration_seconds"])
        self.assertEqual(self.full_row(track), row)

    def test_a_stalled_decoder_is_killed_at_its_timeout(self):
        started = time.monotonic()
        with self.assertRaisesRegex(wg.ResourceLimit, "longer than"):
            wg.decode_bounded([sys.executable, "-c", "import time; time.sleep(60)"], 1024, timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)

    def test_excessive_decoder_output_is_stopped_at_the_cap(self):
        with self.assertRaisesRegex(wg.ResourceLimit, "decodes to more than"):
            wg.decode_bounded([sys.executable, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"],
                              2 * 1024 * 1024, timeout=30)

    def test_the_bounded_decoder_produces_the_analyzers_exact_pcm(self):
        path = _audio(self.media / "same.flac")
        self.assertEqual(wg.decode_bounded(analyze_tracks.pcm_command(path, 4410, 1), 10 ** 8, 60),
                         analyze_tracks.decode_audio_to_pcm(path, 4410))
        self.assertEqual(wg.decode_bounded(analyze_tracks.pcm_command(path, 4410, 2), 10 ** 8, 60),
                         analyze_tracks.decode_audio_to_pcm_stereo(path, 4410))

    def assert_failed_safely(self, track, old_cache, row):
        self.assertEqual(self.cache(track).read_bytes(), old_cache)            # previous cache intact
        self.assertEqual(self.full_row(track), row)                            # no Track column changed
        os.close(waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock"))   # lock released
        self.assertEqual([p.name for p in self.waves.iterdir()
                          if p.name not in (waveform_cache.LOCK_DIRNAME, f"{track.pk}.json")], [])

    def test_misleading_duration_metadata_is_caught_while_decoding(self):
        track = self.track(seconds=3.0)
        Track.objects.filter(pk=track.pk).update(duration_seconds=0.5)        # recorded far shorter than it is
        old, row = self.cache(track).read_bytes(), self.full_row(track)
        with patch.object(wg, "DURATION_SLACK_SECONDS", 0.0), patch.object(wg, "DURATION_SLACK_RATIO", 1.0):
            result = wg.refresh(track.pk)
        self.assertEqual(result.status, wg.RESOURCE_LIMIT)
        self.assertIn("decodes to more than", result.detail)
        self.assert_failed_safely(track, old, row)

    def test_a_timeout_during_refresh_fails_safely_and_cleans_up(self):
        track = self.track()
        old, row = self.cache(track).read_bytes(), self.full_row(track)
        with patch.object(wg, "decode_timeout", return_value=0.0001):
            result = wg.refresh(track.pk)
        self.assertEqual(result.status, wg.RESOURCE_LIMIT)
        self.assert_failed_safely(track, old, row)

    def test_a_limit_failure_reaches_the_background_status(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        token = time.time_ns()
        operation = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        slot = waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK)
        request_lock = waveform_cache.create_request_lock(wg.request_lock_name(track.pk, token))
        with patch("os.nice"), patch.object(wg, "decode_timeout", return_value=0.0001):
            with self.assertRaises(Exception):
                call_command("refresh_waveform", str(track.pk), "--background", "--operation-lock-fd", str(operation),
                             "--slot-lock-fd", str(slot), "--requested-ns", str(token),
                             "--request-lock-fd", str(request_lock), stdout=StringIO())
        status = wg.background_status(track.pk, token)
        self.assertEqual((status["state"], status["status"]), ("failed", wg.RESOURCE_LIMIT))
        self.assertEqual(self.cache(track).read_bytes(), old)
        self.assertFalse((waveform_cache.lock_dir() / wg.request_lock_name(track.pk, token)).exists())
        self.assertTrue(SystemEvent.objects.filter(dedupe_key=wg.outcome_key(track.pk, token)).exists())
