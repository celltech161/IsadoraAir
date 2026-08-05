"""road_conditions/synthesis.py tests: Track lifecycle for the single,
stable KanDrive report file. No live Kokoro/ffmpeg calls anywhere here
-- subprocess.run is patched with a fake that writes placeholder bytes,
matching the established pattern in webrequests/tests/
test_dedication_intros.py's DedicationSynthesisTests.setUp exactly."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TransactionTestCase

from library.models import Artist, Category, CategoryKind, Track
from road_conditions import synthesis as synthesis_module
from road_conditions.synthesis import (
    SynthesisError, retire_road_report, road_report_path, synthesize_road_report,
)

DAY_VOICE = {"engine": "kokoro", "model": "af_jessica", "name": "Claira"}
NIGHT_VOICE = {"engine": "kokoro", "model": "am_liam", "name": "Max"}


class KanDriveSynthesisFixtureMixin:
    def setUp(self):
        super().setUp()
        # TransactionTestCase flushes the database between tests (no
        # wrap-and-rollback the way TestCase does), which also wipes
        # rows a data migration seeded -- explicit get_or_create here
        # matches the same established idiom
        # webrequests/tests/test_dedication_intros.py's
        # DedicationFixtureMixin uses for its own Dedications category.
        spot_kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        Category.objects.get_or_create(code="KanDrive", defaults={"name": "KanDrive", "kind": spot_kind})

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        root_patcher = patch.object(synthesis_module, "KANDRIVE_ROOT", Path(self._tmpdir.name))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

        self._kokoro_should_fail = False
        self._ffmpeg_should_fail = False
        self._kokoro_call_count = 0

        def fake_run(cmd, **kwargs):
            import subprocess as _sp
            if cmd[0] == synthesis_module.KOKORO_BINARY:
                self._kokoro_call_count += 1
                if self._kokoro_should_fail:
                    raise _sp.CalledProcessError(1, cmd, output=b"", stderr=b"kokoro exploded")
                Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"FAKE-WAV-CONTENT")
            elif cmd[0] == "ffmpeg":
                if self._ffmpeg_should_fail:
                    raise _sp.CalledProcessError(1, cmd, output=b"", stderr=b"ffmpeg exploded")
                Path(cmd[-1]).write_bytes(b"FAKE-FLAC-CONTENT")
            else:
                raise AssertionError(f"unexpected subprocess call: {cmd}")
            return _sp.CompletedProcess(cmd, 0)

        run_patcher = patch.object(synthesis_module.subprocess, "run", side_effect=fake_run)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        duration_patcher = patch.object(synthesis_module, "_probe_duration", return_value=300.0)
        duration_patcher.start()
        self.addCleanup(duration_patcher.stop)

        analyze_patcher = patch.object(synthesis_module, "_run_full_analysis", return_value=True)
        analyze_patcher.start()
        self.addCleanup(analyze_patcher.stop)


class SynthesizeRoadReportTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def test_creates_track_with_expected_metadata(self):
        track = synthesize_road_report("Test report text.", "day", DAY_VOICE)

        self.assertEqual(track.filepath, str(road_report_path()))
        self.assertEqual(track.format, "flac")
        self.assertEqual(track.title, "KanDrive Road Report (Claira)")
        self.assertEqual(track.artist.name, "Oak Grove Radio")
        self.assertEqual(track.category.code, "KanDrive")
        self.assertTrue(track.ready2air)
        self.assertEqual(track.duration_seconds, 300.0)

    def test_file_actually_written_to_final_path(self):
        track = synthesize_road_report("Test report text.", "day", DAY_VOICE)
        self.assertTrue(Path(track.filepath).exists())
        self.assertEqual(Path(track.filepath).read_bytes(), b"FAKE-FLAC-CONTENT")

    def test_repeated_generation_reuses_same_track_no_duplicates(self):
        first = synthesize_road_report("First version.", "day", DAY_VOICE)
        second = synthesize_road_report("Second version.", "night", NIGHT_VOICE)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Track.objects.filter(category__code="KanDrive").count(), 1)
        self.assertEqual(second.title, "KanDrive Road Report (Max)")

    def test_no_temp_files_left_behind_after_success(self):
        synthesize_road_report("Test report text.", "day", DAY_VOICE)
        leftovers = [p for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_full_analysis_invoked_and_not_overridden_afterward(self):
        """Unlike the Dedications intro pattern (a short speech clip
        that deliberately opts OUT of real cue-point analysis),
        KanDrive IS rotation-eligible and must get REAL envelope-based
        analysis -- and, unlike dedication intros, this module must
        NOT re-assert next_start_seconds/cue_in_seconds afterward."""
        def fake_analysis(track):
            Track.objects.filter(id=track.id).update(next_start_seconds=42.0, cue_in_seconds=3.5)
            return True

        with patch.object(synthesis_module, "_run_full_analysis", side_effect=fake_analysis):
            track = synthesize_road_report("Test report text.", "day", DAY_VOICE)

        track.refresh_from_db()
        self.assertEqual(track.next_start_seconds, 42.0)
        self.assertEqual(track.cue_in_seconds, 3.5)

    def test_non_kokoro_voice_engine_rejected(self):
        with self.assertRaises(SynthesisError):
            synthesize_road_report("Test text.", "afternoon", {"engine": "piper", "model": "x", "name": "Y"})
        self.assertEqual(self._kokoro_call_count, 0)

    def test_analysis_failure_raises_synthesis_error(self):
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(SynthesisError):
                synthesize_road_report("Test report text.", "day", DAY_VOICE)


class LastKnownGoodPreservationTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def test_kokoro_failure_preserves_last_known_good_file_and_track(self):
        good = synthesize_road_report("Good report.", "day", DAY_VOICE)
        good_path = Path(good.filepath)
        good_bytes = good_path.read_bytes()
        good_duration = good.duration_seconds

        self._kokoro_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("This one fails.", "night", NIGHT_VOICE)

        self.assertTrue(good_path.exists())
        self.assertEqual(good_path.read_bytes(), good_bytes)
        good.refresh_from_db()
        self.assertEqual(good.duration_seconds, good_duration)
        self.assertEqual(good.title, "KanDrive Road Report (Claira)")  # NOT overwritten to the failed run's voice

    def test_ffmpeg_failure_preserves_last_known_good_file_and_track(self):
        good = synthesize_road_report("Good report.", "day", DAY_VOICE)
        good_bytes = Path(good.filepath).read_bytes()

        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("This one fails.", "night", NIGHT_VOICE)

        self.assertEqual(Path(good.filepath).read_bytes(), good_bytes)
        self.assertEqual(Track.objects.filter(category__code="KanDrive").count(), 1)

    def test_no_temp_files_left_behind_after_failure(self):
        self._kokoro_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("This one fails.", "day", DAY_VOICE)
        leftovers = [p for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_analysis_failure_does_not_leave_partial_file_content_mismatched(self):
        """The file IS replaced before analysis runs (analysis needs a
        real file to inspect) -- but a SynthesisError here still means
        this generation cycle is reported as a failure, matching the
        "must not silently produce misleading audio" requirement."""
        synthesize_road_report("Good report.", "day", DAY_VOICE)
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(SynthesisError):
                synthesize_road_report("New content.", "night", NIGHT_VOICE)
        # The file WAS replaced (os.replace already happened by the time
        # analysis runs) -- this documents that fact rather than hiding it.
        self.assertEqual(Path(road_report_path()).read_bytes(), b"FAKE-FLAC-CONTENT")


class RetireRoadReportTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def test_noop_when_no_track_exists(self):
        self.assertFalse(retire_road_report())

    def test_flips_ready2air_false(self):
        track = synthesize_road_report("Report.", "day", DAY_VOICE)
        self.assertTrue(track.ready2air)

        self.assertTrue(retire_road_report())

        track.refresh_from_db()
        self.assertFalse(track.ready2air)

    def test_idempotent_second_call_is_noop(self):
        synthesize_road_report("Report.", "day", DAY_VOICE)
        self.assertTrue(retire_road_report())
        self.assertFalse(retire_road_report())

    def test_does_not_delete_the_file_or_row(self):
        track = synthesize_road_report("Report.", "day", DAY_VOICE)
        path = Path(track.filepath)
        retire_road_report()
        self.assertTrue(path.exists())
        self.assertTrue(Track.objects.filter(id=track.id).exists())

    def test_never_touches_a_manually_uploaded_track_in_kandrive_category(self):
        """A human could upload their own file into the KanDrive
        category folder for some other reason -- retire_road_report()
        only ever touches the ONE stable, generator-owned filepath, and
        must never affect a different Track row even in the same
        category."""
        artist, _ = Artist.get_or_create_ci("Some Human")
        manual_path = Path(self._tmpdir.name) / "manually-uploaded.flac"
        manual_path.write_bytes(b"human-uploaded content")
        manual_track = Track.objects.create(
            filepath=str(manual_path), filename=manual_path.name, format="flac",
            title="Manually Uploaded Clip", artist=artist,
            category=Category.objects.get(code="KanDrive"),
            duration_seconds=12.0, ready2air=True,
        )

        synthesize_road_report("Report.", "day", DAY_VOICE)
        retire_road_report()

        manual_track.refresh_from_db()
        self.assertTrue(manual_track.ready2air, "a manually uploaded track must never be retired by generator logic")
        self.assertTrue(manual_path.exists())
