"""P0 media-attribution queue, classification, and process-bound tests."""

import json
import os
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core import mail
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from library.models import Artist, MediaPlaybackIncident, Track
from library.services import media_health
from monitoring.models import NotificationConfig, SystemEvent


def _write_wav(path, frames=4410):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"".join(struct.pack("<h", 500) for _ in range(frames)))


def _identity(path):
    stat = os.stat(path)
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _detail(ffprobe="ok", ffmpeg="ok", gst_status="ok", gst_probe="eos"):
    return {
        "ffprobe": {"status": ffprobe},
        "ffmpeg": {"status": ffmpeg},
        "gstreamer": {"status": gst_status, "probe": {"status": gst_probe, "eos": gst_probe == "eos"}},
    }


class EvidenceCaptureTests(SimpleTestCase):
    def test_capture_is_in_memory_only_and_generation_specific(self):
        track = SimpleNamespace(
            pk=31, title="Snapshot title", filepath="/media/not-touched.wav",
            duration_seconds=42.0,
        )
        deck = SimpleNamespace(
            track=track, slot="B", generation=9, observed_duration_seconds=43.0,
            milestone_snapshot=MagicMock(return_value={
                "generation": 9, "media_buffers": 123,
                "last_media_buffer_age_seconds": 2.5,
                "milestones": {"A_DECODER_AUDIO_EOS": {"count": 1, "last_ms": 5}},
            }),
        )
        with patch.object(media_health.os, "stat", side_effect=AssertionError("capture must not stat")):
            evidence = media_health.capture_deck_evidence(
                deck, trigger="watchdog_stall", runtime_commit="abc", position_seconds=41.5,
            )
        self.assertEqual(evidence["deck_generation"], 9)
        self.assertEqual(evidence["media_buffer_count"], 123)
        self.assertEqual(evidence["eos_snapshot"]["generation"], 9)
        self.assertNotIn("file_size_snapshot", evidence)


class ClassificationTests(SimpleTestCase):
    def _incident(self, path, *, eos=None):
        identity = _identity(path)
        return SimpleNamespace(
            filepath_snapshot=str(path), eos_snapshot=eos or {},
            file_exists_snapshot=True, file_size_snapshot=identity["size"],
            file_mtime_ns_snapshot=identity["mtime_ns"],
        ), identity

    def test_classification_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)
            incident, identity = self._incident(path)
            cases = (
                (_detail(ffprobe="failed"), MediaPlaybackIncident.CLASS_CONFIRMED_MEDIA_FAILURE),
                (_detail(ffmpeg="failed"), MediaPlaybackIncident.CLASS_CONFIRMED_MEDIA_FAILURE),
                (_detail(gst_probe="error"), MediaPlaybackIncident.CLASS_GSTREAMER_COMPATIBILITY),
                (_detail(gst_status="timeout", gst_probe=""), MediaPlaybackIncident.CLASS_GSTREAMER_COMPATIBILITY),
                (_detail(), MediaPlaybackIncident.CLASS_VALIDATION_CLEAN),
                (_detail(ffprobe="timeout"), MediaPlaybackIncident.CLASS_INCONCLUSIVE),
            )
            for detail, expected in cases:
                with self.subTest(expected=expected):
                    self.assertEqual(media_health.classify_result(incident, detail, identity), expected)

    def test_file_replacement_wins_over_probe_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)
            incident, identity = self._incident(path)
            identity = {**identity, "mtime_ns": identity["mtime_ns"] + 1}
            self.assertEqual(
                media_health.classify_result(incident, _detail(), identity),
                MediaPlaybackIncident.CLASS_FILE_MISSING_OR_CHANGED,
            )

    def test_decoder_and_real_leg_eos_proves_engine_completion_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)
            eos = {"milestones": {
                "A_DECODER_AUDIO_EOS": {"count": 1, "last_ms": 100},
                "B_REAL_LEG_EOS_BEFORE_CONCAT": {"count": 1, "last_ms": 110},
            }}
            incident, identity = self._incident(path, eos=eos)
            self.assertEqual(
                media_health.classify_result(incident, _detail(ffmpeg="failed"), identity),
                MediaPlaybackIncident.CLASS_ENGINE_COMPLETION_PATH,
            )

    def test_rejected_post_seek_eos_is_not_completion_path_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            _write_wav(path)
            eos = {"milestones": {
                "A_DECODER_AUDIO_EOS": {"count": 1, "last_ms": 100},
                "B_REAL_LEG_EOS_BEFORE_CONCAT": {"count": 1, "last_ms": 110},
                "I_EOS_REJECTED_POST_SEEK": {"count": 1, "last_ms": 120},
            }}
            incident, identity = self._incident(path, eos=eos)
            self.assertEqual(
                media_health.classify_result(incident, _detail(), identity),
                MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            )


class BoundedSubprocessTests(SimpleTestCase):
    def test_timeout_terminates_and_reaps_child(self):
        result = media_health.run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.1,
        )
        self.assertEqual(result["status"], "timeout")
        self.assertIsNotNone(result["returncode"])
        self.assertLess(result["duration_seconds"], 3.0)

    def test_stdout_and_stderr_are_bounded_while_pipes_are_fully_drained(self):
        code = "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)"
        result = media_health.run_bounded_command([sys.executable, "-c", code], timeout_seconds=5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["stdout"].encode()), media_health.OUTPUT_LIMIT_BYTES)
        self.assertEqual(len(result["stderr"].encode()), media_health.OUTPUT_LIMIT_BYTES)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])

    def test_argv_is_never_interpreted_by_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            literal = f";touch {marker}"
            result = media_health.run_bounded_command(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", literal],
                timeout_seconds=5,
            )
            self.assertEqual(result["status"], "ok")
            self.assertIn(literal, result["stdout"])
            self.assertFalse(marker.exists())

    def test_each_validator_has_an_explicit_hard_timeout(self):
        stopped = threading.Event()
        with patch.object(media_health, "run_bounded_command", return_value={
            "status": "timeout", "stdout": "", "stderr": "", "returncode": -9,
        }) as run:
            media_health._ffprobe("/tmp/example", stopped)
            self.assertEqual(run.call_args.kwargs["timeout_seconds"], media_health.FFPROBE_TIMEOUT_SECONDS)
            media_health._ffmpeg_decode("/tmp/example", stopped)
            self.assertEqual(run.call_args.kwargs["timeout_seconds"], media_health.FFMPEG_TIMEOUT_SECONDS)
            media_health._gstreamer_decode("/tmp/example", stopped)
            self.assertEqual(run.call_args.kwargs["timeout_seconds"], media_health.GSTREAMER_HARD_TIMEOUT_SECONDS)


class MediaIncidentDatabaseTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "track.wav"
        _write_wav(self.path)
        artist = Artist.objects.create(name="Validator Artist")
        self.track = Track.objects.create(
            filepath=str(self.path), filename=self.path.name,
            title="Validator Track", artist=artist, duration_seconds=0.1,
            ready2air=True, play_count=7, cue_in_seconds=1.25,
            rotation_weight=4,
        )
        config = NotificationConfig.load()
        config.enabled = True
        config.recipients = "ops@example.com"
        config.cooldown_minutes = 30
        config.save()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _incident(self, **overrides):
        identity = _identity(self.path)
        values = dict(
            track=self.track, track_id_snapshot=self.track.pk,
            track_title_snapshot=self.track.title,
            track_artist_snapshot=self.track.artist.name,
            filepath_snapshot=str(self.path), slot="A", deck_generation=3,
            trigger=MediaPlaybackIncident.TRIGGER_WATCHDOG,
            file_exists_snapshot=True, file_size_snapshot=identity["size"],
            file_mtime_ns_snapshot=identity["mtime_ns"],
        )
        values.update(overrides)
        return MediaPlaybackIncident.objects.create(**values)

    def test_incident_database_failure_is_contained(self):
        evidence = {
            "filepath_snapshot": str(self.path), "slot": "A", "deck_generation": 1,
            "trigger": MediaPlaybackIncident.TRIGGER_WATCHDOG,
        }
        with patch.object(MediaPlaybackIncident.objects, "create", side_effect=RuntimeError("db unavailable")), \
             patch.object(media_health, "emit_event") as event:
            self.assertIsNone(media_health.create_incident(evidence))
        event.assert_called_once()
        self.assertLess(len(event.call_args.kwargs["detail"]["error"]), 2049)

    def test_validation_persists_result_event_and_email_without_mutating_track(self):
        incident = self._incident()
        with patch.object(media_health, "_ffprobe", return_value={"status": "ok"}), \
             patch.object(media_health, "_ffmpeg_decode", return_value={"status": "ok"}), \
             patch.object(media_health, "_gstreamer_decode", return_value={
                 "status": "ok", "probe": {"status": "eos", "eos": True},
             }), \
             patch.object(media_health, "send_operational_email", return_value="sent") as send:
            result = media_health.validate_incident(incident)
        self.assertEqual(result, MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)
        incident.refresh_from_db()
        self.assertEqual(incident.validation_state, MediaPlaybackIncident.STATE_COMPLETE)
        self.assertEqual(incident.notification_status, MediaPlaybackIncident.NOTIFY_SENT)
        self.assertIsNotNone(incident.notified_at)
        self.assertTrue(SystemEvent.objects.filter(
            dedupe_key=f"media-health|validation|incident={incident.pk}",
        ).exists())
        self.track.refresh_from_db()
        self.assertTrue(self.track.ready2air)
        self.assertEqual(self.track.play_count, 7)
        self.assertEqual(self.track.cue_in_seconds, 1.25)
        self.assertEqual(self.track.rotation_weight, 4)
        self.assertIsNone(self.track.category_id)
        send.assert_called_once()

    def test_missing_file_skips_tools_and_is_truthfully_classified(self):
        incident = self._incident()
        self.path.unlink()
        with patch.object(media_health, "_ffprobe") as ffprobe, \
             patch.object(media_health, "send_operational_email", return_value="disabled"):
            result = media_health.validate_incident(incident)
        self.assertEqual(result, MediaPlaybackIncident.CLASS_FILE_MISSING_OR_CHANGED)
        ffprobe.assert_not_called()

    def test_file_changed_during_validation_is_not_claimed_as_validated_bytes(self):
        incident = self._incident()

        def replace_during_decode(_path, _stop):
            with self.path.open("ab") as output:
                output.write(b"replacement")
            return {"status": "ok"}

        with patch.object(media_health, "_ffprobe", return_value={"status": "ok"}), \
             patch.object(media_health, "_ffmpeg_decode", side_effect=replace_during_decode), \
             patch.object(media_health, "_gstreamer_decode", return_value={
                 "status": "ok", "probe": {"status": "eos", "eos": True},
             }), \
             patch.object(media_health, "send_operational_email", return_value="disabled"):
            result = media_health.validate_incident(incident)
        self.assertEqual(result, MediaPlaybackIncident.CLASS_FILE_MISSING_OR_CHANGED)

    def test_cooldown_is_durable_by_file_identity_and_classification(self):
        first = self._incident(
            classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            validation_state=MediaPlaybackIncident.STATE_COMPLETE,
        )
        identity = _identity(self.path)
        token = media_health._notification_identity(first, first.classification, identity)
        first.notification_identity = token
        first.notification_status = MediaPlaybackIncident.NOTIFY_SENT
        first.notified_at = timezone.now()
        first.save(update_fields=["notification_identity", "notification_status", "notified_at"])
        second = self._incident(
            classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            validation_state=MediaPlaybackIncident.STATE_COMPLETE,
        )
        with patch.object(media_health, "send_operational_email") as send:
            second_token, status, notified_at = media_health._notify_incident(second, identity)
        self.assertEqual(second_token, token)
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_SUPPRESSED)
        self.assertIsNone(notified_at)
        send.assert_not_called()

    def test_disabled_media_notifications_do_not_send(self):
        config = NotificationConfig.load()
        config.enabled = False
        config.save(update_fields=["enabled"])
        incident = self._incident(classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)
        with patch.object(media_health, "send_operational_email") as send:
            _token, status, notified_at = media_health._notify_incident(incident, _identity(self.path))
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_DISABLED)
        self.assertIsNone(notified_at)
        send.assert_not_called()

    def test_enabled_media_notification_uses_existing_recipients(self):
        incident = self._incident(
            classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            validation_state=MediaPlaybackIncident.STATE_COMPLETE,
            validator_detail=_detail(),
        )
        _token, status, notified_at = media_health._notify_incident(incident, _identity(self.path))
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_SENT)
        self.assertIsNotNone(notified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])
        self.assertIn("Media tests clean", mail.outbox[0].subject)
        self.assertIn("Track.ready2air was NOT changed", mail.outbox[0].body)

    def test_no_recipient_media_notifications_do_not_send(self):
        config = NotificationConfig.load()
        config.recipients = ""
        config.save(update_fields=["recipients"])
        incident = self._incident(classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)
        with patch.object(media_health, "send_operational_email") as send:
            _token, status, notified_at = media_health._notify_incident(incident, _identity(self.path))
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_NO_RECIPIENTS)
        self.assertIsNone(notified_at)
        send.assert_not_called()

    def test_replaced_file_gets_a_new_notification_identity(self):
        incident = self._incident(classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)
        old = _identity(self.path)
        old_token = media_health._notification_identity(incident, incident.classification, old)
        new = {**old, "mtime_ns": old["mtime_ns"] + 1}
        self.assertNotEqual(
            old_token,
            media_health._notification_identity(incident, incident.classification, new),
        )

    def test_replaced_file_is_eligible_for_fresh_notification(self):
        first = self._incident(
            classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            validation_state=MediaPlaybackIncident.STATE_COMPLETE,
        )
        old = _identity(self.path)
        first.notification_identity = media_health._notification_identity(first, first.classification, old)
        first.notification_status = MediaPlaybackIncident.NOTIFY_SENT
        first.notified_at = timezone.now()
        first.save(update_fields=["notification_identity", "notification_status", "notified_at"])
        second = self._incident(
            classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN,
            validation_state=MediaPlaybackIncident.STATE_COMPLETE,
        )
        replacement = {**old, "size": old["size"] + 1, "mtime_ns": old["mtime_ns"] + 1}
        with patch.object(media_health, "send_operational_email", return_value="sent") as send:
            _token, status, notified_at = media_health._notify_incident(second, replacement)
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_SENT)
        self.assertIsNotNone(notified_at)
        send.assert_called_once()

    def test_notification_infrastructure_failure_does_not_change_validation_result(self):
        incident = self._incident(classification=MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)
        with patch.object(media_health.NotificationConfig, "load", side_effect=RuntimeError("config db down")):
            token, status, notified_at = media_health._notify_incident(incident, _identity(self.path))
        self.assertTrue(token)
        self.assertEqual(status, MediaPlaybackIncident.NOTIFY_FAILED)
        self.assertIsNone(notified_at)
        self.assertEqual(incident.classification, MediaPlaybackIncident.CLASS_VALIDATION_CLEAN)

    def test_worker_recovers_interrupted_rows_and_claims_oldest_first(self):
        older = self._incident(validation_state=MediaPlaybackIncident.STATE_VALIDATING)
        newer = self._incident()
        worker = media_health.MediaValidationWorker()
        worker._recover_interrupted()
        older.refresh_from_db()
        self.assertEqual(older.validation_state, MediaPlaybackIncident.STATE_PENDING)
        claimed = worker._claim_next()
        self.assertEqual(claimed.pk, older.pk)
        older.refresh_from_db()
        self.assertEqual(older.validation_attempts, 1)
        newer.refresh_from_db()
        self.assertEqual(newer.validation_state, MediaPlaybackIncident.STATE_PENDING)

    def test_worker_start_is_singleton_and_stop_is_nonblocking(self):
        worker = media_health.MediaValidationWorker()
        gate = threading.Event()
        with patch.object(worker, "_run", side_effect=gate.wait):
            worker.start()
            first = worker._thread
            worker.start()
            self.assertIs(worker._thread, first)
            started = time.monotonic()
            worker.stop()
            self.assertLess(time.monotonic() - started, 0.05)
            gate.set()
            first.join(timeout=1)

    def test_multiple_pending_incidents_are_processed_serially(self):
        first = self._incident()
        second = self._incident()
        seen = []
        worker = media_health.MediaValidationWorker()

        def complete(incident, **_kwargs):
            seen.append(incident.pk)
            MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
                validation_state=MediaPlaybackIncident.STATE_COMPLETE,
            )

        with patch.object(media_health, "validate_incident", side_effect=complete):
            self.assertTrue(worker._process_once())
            self.assertTrue(worker._process_once())
        self.assertEqual(seen, [first.pk, second.pk])

    def test_worker_exception_persists_inconclusive_and_notifies(self):
        incident = self._incident()
        worker = media_health.MediaValidationWorker()
        with patch.object(media_health, "validate_incident", side_effect=RuntimeError("validator boom")), \
             patch.object(media_health, "send_operational_email", return_value="sent"):
            self.assertTrue(worker._process_once())
        incident.refresh_from_db()
        self.assertEqual(incident.validation_state, MediaPlaybackIncident.STATE_COMPLETE)
        self.assertEqual(incident.classification, MediaPlaybackIncident.CLASS_INCONCLUSIVE)
        self.assertEqual(incident.notification_status, MediaPlaybackIncident.NOTIFY_SENT)
        self.assertTrue(SystemEvent.objects.filter(
            dedupe_key=f"media-health|validation|incident={incident.pk}",
        ).exists())

    def test_shutdown_interruption_returns_incident_to_pending(self):
        incident = self._incident()
        worker = media_health.MediaValidationWorker()
        with patch.object(media_health, "validate_incident", side_effect=media_health.ValidationInterrupted):
            self.assertTrue(worker._process_once())
        incident.refresh_from_db()
        self.assertEqual(incident.validation_state, MediaPlaybackIncident.STATE_PENDING)


class RealValidatorTopologyTests(SimpleTestCase):
    def test_valid_wav_reaches_eos_in_isolated_gstreamer_child(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.wav"
            _write_wav(path)
            result = media_health._gstreamer_decode(str(path), threading.Event())
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["probe"]["status"], "eos", result)
        self.assertGreater(result["probe"]["buffers"], 0)

    def test_generated_wav_passes_all_three_real_validators(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.wav"
            _write_wav(path)
            stop = threading.Event()
            ffprobe = media_health._ffprobe(str(path), stop)
            ffmpeg = media_health._ffmpeg_decode(str(path), stop)
            gstreamer = media_health._gstreamer_decode(str(path), stop)
        self.assertEqual(ffprobe["status"], "ok", ffprobe)
        self.assertEqual(ffmpeg["status"], "ok", ffmpeg)
        self.assertEqual(gstreamer["probe"]["status"], "eos", gstreamer)

    def test_tiny_valid_wav_passes_complete_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tiny.wav"
            _write_wav(path, frames=1)
            result = media_health._ffmpeg_decode(str(path), threading.Event())
        self.assertEqual(result["status"], "ok", result)

    def test_generated_mp3_passes_all_three_real_validators_when_encoder_available(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "source.wav"
            mp3_path = Path(directory) / "valid.mp3"
            _write_wav(wav_path)
            encoded = media_health.run_bounded_command([
                "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
                "-i", str(wav_path), "-codec:a", "libmp3lame", str(mp3_path),
            ], timeout_seconds=10)
            if encoded["status"] != "ok":
                self.skipTest(f"system ffmpeg MP3 encoder unavailable: {encoded['stderr']}")
            stop = threading.Event()
            ffprobe = media_health._ffprobe(str(mp3_path), stop)
            ffmpeg = media_health._ffmpeg_decode(str(mp3_path), stop)
            gstreamer = media_health._gstreamer_decode(str(mp3_path), stop)
        self.assertEqual(ffprobe["status"], "ok", ffprobe)
        self.assertEqual(ffmpeg["status"], "ok", ffmpeg)
        self.assertEqual(gstreamer["probe"]["status"], "eos", gstreamer)

    def test_empty_and_malformed_media_fail_independent_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.wav"
            malformed = Path(directory) / "malformed.mp3"
            empty.write_bytes(b"")
            malformed.write_bytes(b"not an audio stream")
            for path in (empty, malformed):
                with self.subTest(path=path.name):
                    result = media_health._ffprobe(str(path), threading.Event())
                    self.assertEqual(result["status"], "failed", result)
