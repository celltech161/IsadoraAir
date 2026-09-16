"""Focused preservation tests for Aircheck active-session segmentation.

The real Liquidsoap rename/reopen boundary was also exercised with the
installed Liquidsoap 2.4.0+dev; see docs/AIRCHECK_SEGMENTATION.md. These
tests pin the Python-side ordering, recovery, persistent transfer, async
Stop contract, and lossless four-format ffmpeg finalization.
"""
import json
import os
import subprocess
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase

from aircheck.models import AircheckSession
from aircheck.services import recorder
from aircheck.tests.test_recorder import AircheckRecorderFixtureMixin


class SegmentationFixture(AircheckRecorderFixtureMixin):
    def make_session(self, audio_format="mp3"):
        extension = {"he_aac": "m4a", "mp3": "mp3", "flac": "flac", "wav": "wav"}[audio_format]
        return AircheckSession.objects.create(
            filename=str(self.out_dir / f"logical.{extension}"),
            audio_format=audio_format,
            bitrate="64k" if audio_format == "he_aac" else "320k",
            source_device="airtap",
            still_running=True,
        )

    def reopening_telnet(self, payload=b"new-segment"):
        def reopen(_command):
            self.working_path.write_bytes(payload)
        return reopen


class SafeCutPrimitiveTests(SegmentationFixture, TestCase):
    def test_isolation_identifies_old_inode_without_stopping_session(self):
        session = self.make_session()
        self.working_path.write_bytes(b"old-audio")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()):
            handoff = recorder._isolate_active_working_file(session, 1)
        self.assertEqual(handoff.read_bytes(), b"old-audio")
        session.refresh_from_db()
        self.assertTrue(session.still_running)

    def test_reopen_returns_capture_to_fresh_fixed_path(self):
        session = self.make_session()
        self.working_path.write_bytes(b"old")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet(b"fresh")):
            recorder._isolate_active_working_file(session, 1)
        self.assertEqual(self.working_path.read_bytes(), b"fresh")

    def test_old_segment_and_new_working_path_are_not_swapped(self):
        session = self.make_session()
        self.working_path.write_bytes(b"before-boundary")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet(b"after-boundary")):
            handoff = recorder._isolate_active_working_file(session, 1)
        self.assertEqual(handoff.read_bytes(), b"before-boundary")
        self.assertEqual(self.working_path.read_bytes(), b"after-boundary")

    def test_reopen_failure_restores_only_active_path(self):
        session = self.make_session()
        self.working_path.write_bytes(b"preserve-me")
        with patch.object(recorder, "_send_telnet", side_effect=recorder.TelnetError("down")):
            with self.assertRaises(recorder.SegmentError):
                recorder._isolate_active_working_file(session, 1)
        self.assertEqual(self.working_path.read_bytes(), b"preserve-me")
        self.assertEqual(recorder._pending_handoffs(session), [])

    def test_lost_response_with_new_path_retains_closed_handoff(self):
        session = self.make_session()
        self.working_path.write_bytes(b"closed-source")
        def processed_then_lost(_command):
            self.working_path.write_bytes(b"new-live")
            raise recorder.TelnetError("response lost")
        with patch.object(recorder, "_send_telnet", side_effect=processed_then_lost):
            with self.assertRaises(recorder.SegmentError):
                recorder._isolate_active_working_file(session, 1)
        self.assertEqual(self.working_path.read_bytes(), b"new-live")
        self.assertEqual(recorder._pending_handoffs(session)[0][1].read_bytes(), b"closed-source")

    def test_successful_cut_never_marks_session_stopped(self):
        session = self.make_session()
        self.working_path.write_bytes(b"audio")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()):
            recorder._cut_and_stage_active_segment(session)
        session.refresh_from_db()
        self.assertTrue(session.still_running)
        self.assertIsNone(session.ended_at)


class ActiveContainmentTests(SegmentationFixture, TestCase):
    def test_active_below_limit_is_noop(self):
        self.make_session()
        self.working_path.write_bytes(b"123")
        with patch.object(recorder, "_send_telnet") as telnet:
            self.assertEqual(recorder.maintain_idle_buffer(max_bytes=4), "active_below_limit")
        telnet.assert_not_called()

    def test_active_above_limit_cuts_exactly_one_segment(self):
        session = self.make_session()
        self.working_path.write_bytes(b"12345")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet(b"n")) as telnet:
            self.assertEqual(recorder.maintain_idle_buffer(max_bytes=4), "active_segmented")
        telnet.assert_called_once()
        self.assertEqual([p.name for p in recorder._discover_segments(session)], ["segment-000001.mp3"])

    def test_completed_segment_leaves_run_for_persistent_staging(self):
        session = self.make_session()
        self.working_path.write_bytes(b"source")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()):
            recorder.maintain_idle_buffer(max_bytes=1)
        self.assertEqual(recorder._discover_segments(session)[0].read_bytes(), b"source")
        self.assertEqual(recorder._pending_handoffs(session), [])

    def test_repeated_cycles_create_disk_discoverable_order(self):
        session = self.make_session()
        for index in range(3):
            self.working_path.write_bytes(f"segment-{index}".encode())
            with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet(b"n")):
                recorder.maintain_idle_buffer(max_bytes=1)
        self.assertEqual(
            [p.name for p in recorder._discover_segments(session)],
            ["segment-000001.mp3", "segment-000002.mp3", "segment-000003.mp3"],
        )
        self.assertEqual(list(self.working_path.parent.glob("*.handoff")), [])

    def test_sequence_is_derived_from_disk_after_fresh_lookup(self):
        session = self.make_session()
        staging = recorder._staging_dir(session)
        staging.mkdir(parents=True)
        recorder._segment_path(session, 7).write_bytes(b"seven")
        self.assertEqual(recorder._next_segment_sequence(session), 8)

    def test_staging_stays_bound_to_session_filename_after_config_edit(self):
        session = self.make_session()
        expected = self.out_dir / recorder.STAGING_ROOT_NAME / str(session.id)
        self.cfg.output_directory = str(self.out_dir / "changed")
        self.cfg.save()
        self.assertEqual(recorder._staging_dir(session), expected)

    def test_active_segmentation_keeps_logical_session_running(self):
        session = self.make_session()
        self.working_path.write_bytes(b"oversized")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()):
            recorder.maintain_idle_buffer(max_bytes=1)
        session.refresh_from_db()
        self.assertTrue(session.still_running)
        self.assertIsNone(session.ended_at)

    def test_active_segmentation_leaves_new_fixed_working_path(self):
        self.make_session()
        self.working_path.write_bytes(b"oversized")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet(b"ongoing")):
            recorder.maintain_idle_buffer(max_bytes=1)
        self.assertEqual(self.working_path.read_bytes(), b"ongoing")

    def test_active_lock_contention_is_harmless_and_nonblocking(self):
        self.make_session()
        self.working_path.write_bytes(b"oversized")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with patch.object(recorder, "_send_telnet") as telnet:
                self.assertEqual(recorder.maintain_idle_buffer(max_bytes=1), "lock_busy")
            telnet.assert_not_called()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TransferFailureTests(SegmentationFixture, TestCase):
    def test_staging_preflight_failure_leaves_active_audio_untouched(self):
        session = self.make_session()
        self.working_path.write_bytes(b"only-copy")
        blocker = self.out_dir / "not-a-directory"
        blocker.parent.mkdir(parents=True)
        blocker.write_bytes(b"x")
        with patch.object(recorder, "_staging_dir", return_value=blocker / "child"), \
             patch.object(recorder, "_send_telnet") as telnet:
            result = recorder.maintain_idle_buffer(max_bytes=1)
        self.assertEqual(result, "active_segment_failed")
        self.assertEqual(self.working_path.read_bytes(), b"only-copy")
        telnet.assert_not_called()
        session.refresh_from_db()
        self.assertTrue(session.still_running)

    def test_copy_failure_retains_handoff_and_partial_is_uncommitted(self):
        session = self.make_session()
        self.working_path.write_bytes(b"recoverable")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()), \
             patch.object(recorder.shutil, "copyfileobj", side_effect=OSError("disk full")):
            result = recorder.maintain_idle_buffer(max_bytes=1)
        self.assertEqual(result, "active_segment_failed")
        self.assertEqual(recorder._pending_handoffs(session)[0][1].read_bytes(), b"recoverable")
        self.assertEqual(recorder._discover_segments(session), [])

    def test_partial_destination_is_never_discovered(self):
        session = self.make_session()
        staging = recorder._staging_dir(session)
        staging.mkdir(parents=True)
        (staging / ".segment-000001.mp3.partial").write_bytes(b"half")
        self.assertEqual(recorder._discover_segments(session), [])
        self.assertEqual(recorder._next_segment_sequence(session), 1)

    def test_retry_of_committed_segment_does_not_duplicate(self):
        session = self.make_session()
        staging = recorder._staging_dir(session)
        staging.mkdir(parents=True)
        committed = recorder._segment_path(session, 1)
        committed.write_bytes(b"same")
        handoff = recorder._handoff_path(session, 1)
        handoff.write_bytes(b"same")
        recorder._copy_handoff_to_staging(session, 1, handoff)
        self.assertFalse(handoff.exists())
        self.assertEqual(recorder._discover_segments(session), [committed])

    def test_later_cycle_retries_handoff_even_when_current_is_below_limit(self):
        session = self.make_session()
        self.working_path.write_bytes(b"new")
        handoff = recorder._handoff_path(session, 1)
        handoff.write_bytes(b"closed-source")
        result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "active_segmented")
        self.assertFalse(handoff.exists())
        self.assertEqual(recorder._discover_segments(session)[0].read_bytes(), b"closed-source")

    def test_handoff_and_segments_are_isolated_by_session(self):
        first = self.make_session()
        second = AircheckSession.objects.create(
            filename=str(self.out_dir / "second.mp3"), audio_format="mp3",
            bitrate="320k", source_device="airtap", still_running=False,
        )
        recorder._handoff_path(second, 1).write_bytes(b"other")
        other_staging = recorder._staging_dir(second)
        other_staging.mkdir(parents=True)
        recorder._segment_path(second, 1).write_bytes(b"other")
        self.assertEqual(recorder._pending_handoffs(first), [])
        self.assertEqual(recorder._discover_segments(first), [])


class StopContractTests(SegmentationFixture, TestCase):
    class InspectingThread:
        acquired_outside_lock = None
        target = None
        args = None

        def __init__(self, target, args, **_kwargs):
            type(self).target = target
            type(self).args = args

        def start(self):
            with recorder._aircheck_lock(blocking=False) as acquired:
                type(self).acquired_outside_lock = acquired

    def stage(self, session, sequence, payload):
        path = recorder._segment_path(session, sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def write_wav(self, path, frames=4800):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00\x00" * frames)

    def run_finalizer_thread_inline(self):
        class InlineThread:
            def __init__(self, target, args, **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)
        return InlineThread

    def test_segmented_stop_ends_row_and_dispatches_outside_lock(self):
        session = self.make_session()
        self.stage(session, 1, b"first")
        self.working_path.write_bytes(b"last")
        worker = Mock()
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()), \
             patch.object(recorder, "_segmented_finalize_worker", worker), \
             patch.object(recorder.threading, "Thread", self.InspectingThread):
            stopped, error = recorder.stop_recording()
        self.assertIsNone(error)
        self.assertEqual(stopped.id, session.id)
        stopped.refresh_from_db()
        self.assertFalse(stopped.still_running)
        self.assertIsNotNone(stopped.ended_at)
        self.assertEqual(stopped.exit_note, recorder.FINALIZATION_PENDING_NOTE)
        self.assertTrue(self.InspectingThread.acquired_outside_lock)
        self.assertEqual(len(recorder._discover_segments(stopped)), 2)

    def test_finalization_discovers_committed_segments_in_numeric_order(self):
        session = self.make_session()
        for sequence in (3, 1, 2):
            self.stage(session, sequence, str(sequence).encode())
        self.assertEqual(
            [path.name for path in recorder._discover_segments(session)],
            ["segment-000001.mp3", "segment-000002.mp3", "segment-000003.mp3"],
        )

    def test_short_mp3_preserves_direct_move_fast_path(self):
        session = self.make_session()
        self.working_path.write_bytes(b"short-session")
        with patch.object(recorder, "_send_telnet", return_value="ok"), \
             patch.object(recorder, "_finalize_segment_set") as segmented:
            stopped, error = recorder.stop_recording()
        self.assertIsNone(error)
        self.assertEqual(Path(stopped.filename).read_bytes(), b"short-session")
        segmented.assert_not_called()
        self.assertFalse(recorder._staging_dir(session).exists())

    def test_segmented_pending_note_has_finalizing_classification(self):
        session = self.make_session()
        session.still_running = False
        session.ended_at = session.started_at
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "ended_at", "exit_note"])
        self.assertEqual(recorder.classify_finalization(session), "finalizing")

    def test_new_recording_can_start_while_prior_finalization_is_pending(self):
        session = self.make_session()
        self.stage(session, 1, b"first")
        self.working_path.write_bytes(b"last")
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()), \
             patch.object(recorder.threading, "Thread", self.InspectingThread):
            recorder.stop_recording()
        with patch.object(recorder, "_send_telnet", return_value="ok"):
            later, error = recorder.start_recording()
        self.assertIsNone(error)
        self.assertNotEqual(later.id, session.id)
        self.assertNotEqual(later.filename, session.filename)

    def test_stop_after_one_prior_segment_produces_one_final_file(self):
        session = self.make_session("wav")
        self.write_wav(recorder._segment_path(session, 1))
        self.write_wav(self.working_path)
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()), \
             patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync, \
             patch.object(recorder.threading, "Thread", self.run_finalizer_thread_inline()):
            stopped, error = recorder.stop_recording()
        self.assertIsNone(error)
        stopped.refresh_from_db()
        self.assertTrue(Path(stopped.filename).is_file())
        self.assertEqual(stopped.exit_note, "")
        self.assertGreater(stopped.size_bytes, 0)
        self.assertFalse(recorder._staging_dir(stopped).exists())
        sync.assert_called_once()

    def test_stop_after_multiple_rollovers_produces_one_final_file(self):
        session = self.make_session("wav")
        self.write_wav(recorder._segment_path(session, 1))
        self.write_wav(recorder._segment_path(session, 2))
        self.write_wav(self.working_path)
        with patch.object(recorder, "_send_telnet", side_effect=self.reopening_telnet()), \
             patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_sync_finalized_recording_to_library"), \
             patch.object(recorder.threading, "Thread", self.run_finalizer_thread_inline()):
            stopped, error = recorder.stop_recording()
        self.assertIsNone(error)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", stopped.filename],
            check=True, capture_output=True, text=True,
        )
        self.assertAlmostEqual(float(json.loads(probe.stdout)["format"]["duration"]), 0.3, delta=0.01)
        self.assertFalse(recorder._staging_dir(stopped).exists())

    def test_finalization_failure_keeps_all_committed_sources(self):
        session = self.make_session()
        session.still_running = False
        session.save(update_fields=["still_running"])
        sources = [self.stage(session, i, f"part-{i}".encode()) for i in range(1, 4)]
        failure = subprocess.CalledProcessError(1, ["ffmpeg"], stderr="bad concat")
        with patch.object(recorder.subprocess, "run", side_effect=failure):
            with self.assertRaises(recorder.SegmentError):
                recorder._finalize_segment_set(session, Path(session.filename))
        self.assertTrue(all(path.exists() for path in sources))
        self.assertTrue(recorder._staging_dir(session).exists())

    def test_worker_failure_marks_error_and_preserves_sources(self):
        session = self.make_session()
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        source = self.stage(session, 1, b"source")
        with patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_finalize_segment_set", side_effect=recorder.SegmentError("bad")):
            recorder._segmented_finalize_worker(session.id, session.filename)
        session.refresh_from_db()
        self.assertEqual(recorder.classify_finalization(session), "error")
        self.assertTrue(source.exists())

    def test_worker_success_updates_size_clears_note_and_syncs_once(self):
        session = self.make_session()
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        sync = Mock()
        with patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_finalize_segment_set", return_value=4321), \
             patch.object(recorder, "_sync_finalized_recording_to_library", sync):
            recorder._segmented_finalize_worker(session.id, session.filename)
        session.refresh_from_db()
        self.assertEqual(session.size_bytes, 4321)
        self.assertEqual(session.exit_note, "")
        sync.assert_called_once_with(Path(session.filename))


class FormatAcceptanceTests(SegmentationFixture, TestCase):
    duration_per_segment = 0.35

    def generate_segment(self, session, sequence):
        path = recorder._segment_path(session, sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        common = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"sine=frequency={700 + sequence * 100}:sample_rate=48000:duration={self.duration_per_segment}",
            "-ac", "2",
        ]
        codec_args = {
            "he_aac": ["-c:a", "aac", "-b:a", "64k", "-f", "adts"],
            "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
            "flac": ["-c:a", "flac"],
            "wav": ["-c:a", "pcm_s16le"],
        }[session.audio_format]
        subprocess.run(common + codec_args + [str(path)], check=True, capture_output=True)
        return path

    def probe(self, path):
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=format_name,duration:stream=codec_name,codec_type,duration_ts,time_base",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def assert_format_finalizes(self, audio_format):
        session = self.make_session(audio_format)
        session.still_running = False
        session.save(update_fields=["still_running"])
        for sequence in range(1, 4):
            self.generate_segment(session, sequence)
        size = recorder._finalize_segment_set(session, Path(session.filename))
        dest = Path(session.filename)
        self.assertGreater(size, 0)
        info = self.probe(dest)
        self.assertEqual(info["streams"][0]["codec_type"], "audio")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(dest), "-f", "null", "-"],
            check=True, capture_output=True,
        )
        duration = float(info["format"]["duration"])
        tolerance = 0.18 if audio_format in {"he_aac", "mp3"} else 0.06
        self.assertAlmostEqual(duration, self.duration_per_segment * 3, delta=tolerance)
        if audio_format == "he_aac":
            self.assertIn("mp4", info["format"]["format_name"])
        if audio_format == "wav":
            stream = info["streams"][0]
            # One valid PCM/WAV stream with duration/sample accounting.
            self.assertEqual(stream["codec_name"], "pcm_s16le")
            self.assertGreater(int(stream["duration_ts"]), 0)
        self.assertFalse(recorder._staging_dir(session).exists())

    def test_three_segment_he_aac_to_one_valid_m4a(self):
        self.assert_format_finalizes("he_aac")

    def test_three_segment_mp3_to_one_valid_mp3(self):
        self.assert_format_finalizes("mp3")

    def test_three_segment_flac_to_one_valid_flac(self):
        self.assert_format_finalizes("flac")

    def test_three_segment_wav_to_one_valid_wav(self):
        self.assert_format_finalizes("wav")


class DecodeValidationTests(SegmentationFixture, TestCase):
    """P2 1.13A2 -- ffprobe/container metadata alone can pass on a file
    whose audio data is corrupted partway through; only a real decode
    proves the stream is actually sound. These tests pin the gate
    between concat/remux and source cleanup added in this pass."""

    def _write_metadata_valid_but_decode_broken_mp3(self, path, duration=1.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-c:a", "libmp3lame", "-b:a", "192k", str(path),
            ],
            check=True, capture_output=True,
        )
        # Corrupt only the interior of the compressed data -- CBR MP3's
        # container-level duration is a size/bitrate estimate, not a
        # full-file frame scan, so this reliably still probes as valid
        # metadata while desyncing enough frame sync words to make a
        # real decode fail.
        data = bytearray(path.read_bytes())
        n = len(data)
        for i in range(int(n * 0.4), int(n * 0.6)):
            data[i] = 0xFF
        path.write_bytes(bytes(data))

    def test_metadata_passes_but_decode_fails_on_damaged_audio(self):
        """Exercises the real validation command boundary directly
        against an intentionally damaged file: proves requirement (1)
        of the hardening -- ffprobe alone is not sufficient."""
        candidate = self.out_dir / "damaged.mp3"
        self._write_metadata_valid_but_decode_broken_mp3(candidate)

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                str(candidate),
            ],
            check=True, capture_output=True, text=True,
        )
        probe_data = json.loads(probe.stdout)
        self.assertEqual(probe_data["streams"][0]["codec_type"], "audio")
        self.assertGreater(float(probe_data["format"]["duration"]), 0)

        with self.assertRaises(recorder.SegmentError) as ctx:
            recorder._validate_final_audio(candidate)
        self.assertIn("decode", str(ctx.exception).lower())

    def test_finalization_worker_treats_decode_failure_as_error_and_preserves_everything(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        source = recorder._segment_path(session, 1)
        self._write_metadata_valid_but_decode_broken_mp3(source)
        staging = recorder._staging_dir(session)

        with patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync:
            recorder._segmented_finalize_worker(session.id, session.filename)

        session.refresh_from_db()
        self.assertEqual(recorder.classify_finalization(session), "error")
        self.assertIn("decode", session.exit_note.lower())
        self.assertTrue(source.exists())
        self.assertTrue(staging.exists())
        self.assertFalse(Path(session.filename).exists())
        sync.assert_not_called()

    def test_decode_validate_audio_timeout_is_a_segment_error(self):
        """Direct unit test of the real timeout branch -- proves a
        validation timeout is treated exactly like any other validation
        failure (recoverable), without waiting on a real slow decode."""
        candidate = self.out_dir / "slow.mp3"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"irrelevant-to-this-branch")
        with patch.object(
            recorder.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=5),
        ):
            with self.assertRaises(recorder.SegmentError) as ctx:
                recorder._decode_validate_audio(candidate, duration_seconds=10)
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_finalization_worker_treats_decode_timeout_as_error_and_preserves_everything(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        source = recorder._segment_path(session, 1)
        source.parent.mkdir(parents=True, exist_ok=True)
        # Real, valid audio -- the concat step itself must succeed so
        # this test actually reaches (and exercises) the mocked decode-
        # validation timeout branch rather than failing earlier at concat.
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.3",
                "-c:a", "libmp3lame", "-b:a", "192k", str(source),
            ],
            check=True, capture_output=True,
        )
        staging = recorder._staging_dir(session)

        with patch.object(recorder, "close_old_connections"), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync, \
             patch.object(
                 recorder, "_decode_validate_audio",
                 side_effect=recorder.SegmentError("final output decode validation timed out after 999s"),
             ):
            recorder._segmented_finalize_worker(session.id, session.filename)

        session.refresh_from_db()
        self.assertEqual(recorder.classify_finalization(session), "error")
        self.assertIn("timed out", session.exit_note.lower())
        self.assertTrue(source.exists())
        self.assertTrue(staging.exists())
        self.assertFalse(Path(session.filename).exists())
        sync.assert_not_called()

    def test_decode_validation_timeout_is_duration_derived_with_floor_and_ceiling(self):
        self.assertEqual(
            recorder._decode_validation_timeout(0), recorder.DECODE_VALIDATION_MIN_SECONDS
        )
        self.assertEqual(
            recorder._decode_validation_timeout(recorder.DECODE_VALIDATION_MAX_SECONDS * 1000),
            recorder.DECODE_VALIDATION_MAX_SECONDS,
        )
        mid_duration = 10_000
        expected = mid_duration * recorder.DECODE_VALIDATION_SECONDS_PER_DURATION_SECOND
        self.assertTrue(recorder.DECODE_VALIDATION_MIN_SECONDS < expected < recorder.DECODE_VALIDATION_MAX_SECONDS)
        self.assertAlmostEqual(recorder._decode_validation_timeout(mid_duration), expected)

    def test_valid_audio_still_completes_and_cleans_normally(self):
        """Regression guard: the new decode gate must not reject audio
        that genuinely decodes cleanly -- complements the four existing
        FormatAcceptanceTests, which already re-run under the new gate
        since it is wired into _validate_final_audio unconditionally."""
        session = self.make_session("mp3")
        session.still_running = False
        session.save(update_fields=["still_running"])
        for sequence in range(1, 3):
            path = recorder._segment_path(session, sequence)
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={440 + sequence * 50}:sample_rate=48000:duration=0.3",
                    "-c:a", "libmp3lame", "-b:a", "192k", str(path),
                ],
                check=True, capture_output=True,
            )
        size = recorder._finalize_segment_set(session, Path(session.filename))
        self.assertGreater(size, 0)
        self.assertFalse(recorder._staging_dir(session).exists())


class WavRf64Tests(SegmentationFixture, TestCase):
    """P2 1.13A2 -- make the long-WAV intention explicit rather than
    relying on the installed ffmpeg's default (which, as of the
    ffmpeg 8.0.1 in this environment, defaults -rf64 to "never" --
    verified directly with `ffmpeg -h muxer=wav`, not assumed)."""

    def _write_short_wav_segment(self, session, sequence=1, frames=4800):
        path = recorder._segment_path(session, sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00\x00" * frames)
        return path

    def _spy_on_concat_command(self):
        captured = {}
        real_run = recorder.subprocess.run

        def spy(cmd, *args, **kwargs):
            if cmd and cmd[0] == "ffmpeg" and "concat" in cmd:
                captured["cmd"] = cmd
            return real_run(cmd, *args, **kwargs)

        return captured, spy

    def test_wav_finalization_command_explicitly_requests_rf64_auto(self):
        session = self.make_session("wav")
        session.still_running = False
        session.save(update_fields=["still_running"])
        self._write_short_wav_segment(session)

        captured, spy = self._spy_on_concat_command()
        with patch.object(recorder.subprocess, "run", side_effect=spy):
            recorder._finalize_segment_set(session, Path(session.filename))

        cmd = captured["cmd"]
        self.assertIn("-rf64", cmd)
        self.assertEqual(cmd[cmd.index("-rf64") + 1], "auto")

    def test_non_wav_finalization_does_not_request_rf64(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.save(update_fields=["still_running"])
        path = recorder._segment_path(session, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.3",
                "-c:a", "libmp3lame", "-b:a", "192k", str(path),
            ],
            check=True, capture_output=True,
        )

        captured, spy = self._spy_on_concat_command()
        with patch.object(recorder.subprocess, "run", side_effect=spy):
            recorder._finalize_segment_set(session, Path(session.filename))

        self.assertNotIn("-rf64", captured["cmd"])

    def test_small_wav_output_stays_plain_riff_with_rf64_auto(self):
        session = self.make_session("wav")
        session.still_running = False
        session.save(update_fields=["still_running"])
        self._write_short_wav_segment(session)

        recorder._finalize_segment_set(session, Path(session.filename))

        header = Path(session.filename).read_bytes()[:4]
        self.assertEqual(header, b"RIFF")

    def test_small_wav_pcm_sample_format_unaffected_by_rf64_flag(self):
        session = self.make_session("wav")
        session.still_running = False
        session.save(update_fields=["still_running"])
        self._write_short_wav_segment(session)

        recorder._finalize_segment_set(session, Path(session.filename))

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json",
                session.filename,
            ],
            check=True, capture_output=True, text=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "pcm_s16le")
        self.assertEqual(int(stream["sample_rate"]), 48000)
        self.assertEqual(int(stream["channels"]), 1)
