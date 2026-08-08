"""road_conditions/synthesis.py tests: Track lifecycle for the single,
stable KanDrive report file. No live Kokoro/ffmpeg calls anywhere here
-- subprocess.run is patched with a fake that writes placeholder bytes,
matching the established pattern in webrequests/tests/
test_dedication_intros.py's DedicationSynthesisTests.setUp exactly."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from library.models import Artist, Category, CategoryKind, Track
from road_conditions import synthesis as synthesis_module
from road_conditions.synthesis import (
    LOUDNORM_TARGETS, SynthesisError, _parse_loudnorm_output, build_loudnorm_string,
    retire_road_report, road_report_path, road_report_text_path,
    synthesize_road_report, write_road_report_text,
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
        # TMP_AUDIO_ROOT (real /dev/shm in production -- see synthesis.py's
        # own module docstring) is pointed at the SAME tmpdir as
        # KANDRIVE_ROOT here, not a separate one -- that way the existing
        # "no temp files left behind" tests (which scan
        # Path(self._tmpdir.name).iterdir()) stay meaningful for BOTH the
        # final FLAC's own temp file and the intermediate WAV(s), which
        # now live under a different constant, without needing a second
        # leftover-file check.
        tmp_audio_patcher = patch.object(synthesis_module, "TMP_AUDIO_ROOT", Path(self._tmpdir.name))
        tmp_audio_patcher.start()
        self.addCleanup(tmp_audio_patcher.stop)

        self._kokoro_should_fail = False
        self._ffmpeg_should_fail = False
        self._kokoro_call_count = 0
        # loudnorm's own two-pass analysis call (ends "-f null -", no
        # real output file -- see _run_loudnorm_two_pass) is tracked and
        # faked separately from the encode/concat call below, since it
        # has a genuinely different shape (discards its output) and a
        # genuinely different failure-tolerance contract (a failed/empty
        # analysis falls back to conservative defaults rather than
        # raising -- see _run_loudnorm_two_pass's own docstring).
        self._loudnorm_analyze_call_count = 0
        self._loudnorm_analysis_should_be_empty = False

        def fake_run(cmd, **kwargs):
            import subprocess as _sp
            if cmd[0] == synthesis_module.KOKORO_BINARY:
                self._kokoro_call_count += 1
                if self._kokoro_should_fail:
                    raise _sp.CalledProcessError(1, cmd, output=b"", stderr=b"kokoro exploded")
                Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"FAKE-WAV-CONTENT")
                return _sp.CompletedProcess(cmd, 0)
            elif cmd[0] == "ffmpeg":
                if cmd[-3:] == ["-f", "null", "-"]:
                    # loudnorm analysis pass -- never writes a real file
                    # (discarded via -f null), so it's never subject to
                    # _ffmpeg_should_fail (which simulates the ENCODE
                    # pass genuinely failing); a failed/garbled real
                    # analysis pass would just return empty/unparseable
                    # stderr, which _loudnorm_analysis_should_be_empty
                    # simulates directly.
                    self._loudnorm_analyze_call_count += 1
                    if self._loudnorm_analysis_should_be_empty:
                        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="")
                    fake_json = (
                        '[Parsed_loudnorm_0 @ 0x0] \n{\n'
                        '\t"input_i" : "-18.50",\n'
                        '\t"input_tp" : "-3.20",\n'
                        '\t"input_lra" : "4.10",\n'
                        '\t"input_thresh" : "-29.00",\n'
                        '\t"target_offset" : "0.50"\n}\n'
                    )
                    return _sp.CompletedProcess(cmd, 0, stdout="", stderr=fake_json)
                # Encode/concat pass -- produces the real output file.
                if self._ffmpeg_should_fail:
                    raise _sp.CalledProcessError(1, cmd, output=b"", stderr=b"ffmpeg exploded")
                Path(cmd[-1]).write_bytes(b"FAKE-FLAC-CONTENT")
                return _sp.CompletedProcess(cmd, 0)
            else:
                raise AssertionError(f"unexpected subprocess call: {cmd}")

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


class LoudnormHelperTests(TestCase):
    """build_loudnorm_string()/_parse_loudnorm_output() -- pure
    functions, no subprocess/filesystem involved. LOUDNORM_TARGETS
    itself is checked against the exact numbers requested (matching
    the KNS show ingest script's own target for every other syndicated
    show)."""

    def test_loudnorm_targets_match_kns(self):
        self.assertEqual(LOUDNORM_TARGETS, {"I": -16.0, "TP": -1.0, "LRA": 5.0})

    def test_build_loudnorm_string_no_extra_args(self):
        self.assertEqual(build_loudnorm_string(), "loudnorm=I=-16.0:TP=-1.0:LRA=5.0")

    def test_build_loudnorm_string_with_extra_args(self):
        result = build_loudnorm_string("print_format=json")
        self.assertEqual(result, "loudnorm=I=-16.0:TP=-1.0:LRA=5.0:print_format=json")

    def test_parse_loudnorm_output_quoted_json_form(self):
        text = (
            '[Parsed_loudnorm_0 @ 0x0] \n{\n'
            '\t"input_i" : "-18.50",\n'
            '\t"input_tp" : "-3.20",\n'
            '\t"input_lra" : "4.10",\n'
            '\t"input_thresh" : "-29.00",\n'
            '\t"target_offset" : "0.50"\n}\n'
        )
        analysis = _parse_loudnorm_output(text)
        self.assertEqual(analysis, {
            "input_i": "-18.50", "input_tp": "-3.20", "input_lra": "4.10",
            "input_thresh": "-29.00", "target_offset": "0.50",
        })

    def test_parse_loudnorm_output_missing_fields_returns_partial(self):
        analysis = _parse_loudnorm_output("nothing useful here")
        self.assertEqual(analysis, {})


class LoudnormTwoPassSynthesisTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """The two-pass loudnorm integration inside synthesize_road_report()
    itself -- both the plain (single Kokoro call) and transition-sound
    (multi-segment concat) paths route their encode step through
    _run_loudnorm_two_pass, which fake_run's `-f null -` special case
    (see KanDriveSynthesisFixtureMixin) lets these tests observe
    without needing a real ffmpeg/loudnorm build."""

    def test_plain_path_runs_analyze_then_encode(self):
        synthesize_road_report("Test report text.", "day", DAY_VOICE)
        self.assertEqual(self._loudnorm_analyze_call_count, 1)

    def test_transition_path_analyzes_once_for_whole_concatenated_program(self):
        # Not once per segment -- loudnorm measures/corrects the WHOLE
        # finished program together, exactly like KNS measures its own
        # bumper+stories+wooshes as one unit, not each piece separately.
        synthesize_road_report(
            "unused when segments given", "day", DAY_VOICE,
            segments=["first item.", "second item.", "third item."],
            transition_sound_path="/fake/path/woosh.wav",
        )
        self.assertEqual(self._loudnorm_analyze_call_count, 1)
        self.assertEqual(self._kokoro_call_count, 3)

    def test_empty_analysis_falls_back_to_defaults_and_still_succeeds(self):
        self._loudnorm_analysis_should_be_empty = True
        track = synthesize_road_report("Test report text.", "day", DAY_VOICE)
        self.assertTrue(Path(track.filepath).exists())
        self.assertEqual(Path(track.filepath).read_bytes(), b"FAKE-FLAC-CONTENT")

    def test_encode_pass_failure_still_raises_synthesis_error(self):
        # The analyze pass succeeding doesn't matter if the ENCODE pass
        # (the one that actually produces output) fails -- unlike the
        # analyze pass, this is not forgiving.
        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("Test report text.", "day", DAY_VOICE)

class TmpAudioRootProductionValueTests(TestCase):
    """Deliberately does NOT use KanDriveSynthesisFixtureMixin (which
    patches TMP_AUDIO_ROOT to a tmpdir for the rest of this file) --
    checks the REAL, unpatched production value of the constant itself:
    genuine tmpfs, and genuinely distinct from KANDRIVE_ROOT's real
    disk path, matching the module docstring's own claim."""

    def test_tmp_audio_root_is_under_dev_shm(self):
        self.assertEqual(str(synthesis_module.TMP_AUDIO_ROOT.parent), "/dev/shm")

    def test_tmp_audio_root_differs_from_kandrive_root(self):
        self.assertNotEqual(synthesis_module.TMP_AUDIO_ROOT, synthesis_module.KANDRIVE_ROOT)


class TransitionSoundSynthesisTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """synthesize_road_report()'s `segments`/`transition_sound_path`
    parameters -- the item-transition-sound path. fake_run (see
    KanDriveSynthesisFixtureMixin) already handles both an arbitrary
    number of Kokoro calls and either ffmpeg invocation shape (plain
    two-arg convert, or the filter_complex concat used here) generically
    -- both write their output to whatever path is last in the argv --
    so no fixture changes were needed for this class. transition_sound_path
    itself doesn't need to exist on disk for these tests: ffmpeg is
    mocked, so nothing ever actually reads it."""

    FAKE_WOOSH = "/fake/path/woosh.wav"

    def test_multiple_segments_makes_one_kokoro_call_per_segment(self):
        synthesize_road_report(
            "unused when segments given", "day", DAY_VOICE,
            segments=["first item.", "second item.", "third item."],
            transition_sound_path=self.FAKE_WOOSH,
        )
        self.assertEqual(self._kokoro_call_count, 3)

    def test_single_segment_falls_back_to_plain_path(self):
        # Only one item -- no boundary to insert a transition at, so
        # this must behave exactly like the plain (no-segments) path:
        # exactly one Kokoro call, using `text` (not `segments[0]`).
        synthesize_road_report(
            "the actual text sent", "day", DAY_VOICE,
            segments=["only item"], transition_sound_path=self.FAKE_WOOSH,
        )
        self.assertEqual(self._kokoro_call_count, 1)

    def test_none_transition_sound_path_falls_back_to_plain_path_even_with_segments(self):
        synthesize_road_report(
            "the actual text sent", "day", DAY_VOICE,
            segments=["first item.", "second item."], transition_sound_path=None,
        )
        self.assertEqual(self._kokoro_call_count, 1)

    def test_none_segments_falls_back_to_plain_path(self):
        synthesize_road_report(
            "the actual text sent", "day", DAY_VOICE,
            segments=None, transition_sound_path=self.FAKE_WOOSH,
        )
        self.assertEqual(self._kokoro_call_count, 1)

    def test_transition_path_still_creates_track_with_expected_metadata(self):
        track = synthesize_road_report(
            "unused when segments given", "day", DAY_VOICE,
            segments=["first item.", "second item."], transition_sound_path=self.FAKE_WOOSH,
        )
        self.assertEqual(track.filepath, str(road_report_path()))
        self.assertEqual(track.format, "flac")
        self.assertEqual(track.title, "KanDrive Road Report (Claira)")
        self.assertTrue(track.ready2air)
        self.assertEqual(track.duration_seconds, 300.0)  # from the mocked _probe_duration

    def test_transition_path_file_actually_written_to_final_path(self):
        track = synthesize_road_report(
            "unused when segments given", "day", DAY_VOICE,
            segments=["first item.", "second item."], transition_sound_path=self.FAKE_WOOSH,
        )
        self.assertTrue(Path(track.filepath).exists())
        self.assertEqual(Path(track.filepath).read_bytes(), b"FAKE-FLAC-CONTENT")

    def test_no_temp_files_left_behind_after_segmented_success(self):
        synthesize_road_report(
            "unused when segments given", "day", DAY_VOICE,
            segments=["a", "b", "c"], transition_sound_path=self.FAKE_WOOSH,
        )
        leftovers = [p for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_kokoro_failure_on_any_segment_raises_synthesis_error(self):
        self._kokoro_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report(
                "unused when segments given", "day", DAY_VOICE,
                segments=["a", "b"], transition_sound_path=self.FAKE_WOOSH,
            )

    def test_concat_ffmpeg_failure_raises_synthesis_error(self):
        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report(
                "unused when segments given", "day", DAY_VOICE,
                segments=["a", "b"], transition_sound_path=self.FAKE_WOOSH,
            )

    def test_no_temp_files_left_behind_after_segmented_failure(self):
        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report(
                "unused when segments given", "day", DAY_VOICE,
                segments=["a", "b"], transition_sound_path=self.FAKE_WOOSH,
            )
        leftovers = [p for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_segmented_failure_preserves_last_known_good_file_and_track(self):
        good = synthesize_road_report("Good, plain report.", "day", DAY_VOICE)
        good_bytes = Path(good.filepath).read_bytes()

        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report(
                "unused when segments given", "night", NIGHT_VOICE,
                segments=["a", "b"], transition_sound_path=self.FAKE_WOOSH,
            )

        self.assertEqual(Path(good.filepath).read_bytes(), good_bytes)
        good.refresh_from_db()
        self.assertEqual(good.title, "KanDrive Road Report (Claira)")  # NOT overwritten to the failed run's voice

    def test_existing_call_sites_unaffected_by_new_optional_parameters(self):
        # Backward-compatibility guard: every pre-existing call site in
        # this test file (and the real command) calls
        # synthesize_road_report(text, slot, voice) with no segments/
        # transition_sound_path at all -- confirms that 3-positional-arg
        # form still behaves identically (one Kokoro call).
        synthesize_road_report("Plain text, old-style call.", "day", DAY_VOICE)
        self.assertEqual(self._kokoro_call_count, 1)


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


class SynthesizeRoadReportNeverWritesTextFileTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """synthesize_road_report() itself must NEVER write or touch
    road_report.txt -- that responsibility belongs entirely to the
    CALLER (generate_road_condition_audio.py), which calls
    write_road_report_text(text) only after synthesize_road_report()
    has already returned a Track without raising (see this module's own
    comment at the os.replace() call site). This is what makes
    road_report.txt represent only a generation cycle that completed
    successfully start to finish, INCLUDING waveform analysis -- not
    merely a FLAC that was written before a later Track/analysis
    failure. An earlier revision of this function wrote the text file
    itself, immediately after the FLAC's own os.replace() -- these
    tests replace that revision's own (now-wrong) assertions."""

    def test_successful_synthesis_does_not_create_text_file(self):
        synthesize_road_report("Good report.", "day", DAY_VOICE)
        self.assertFalse(road_report_text_path().exists())

    def test_repeated_successful_synthesis_still_never_writes_text_file(self):
        synthesize_road_report("First.", "day", DAY_VOICE)
        synthesize_road_report("Second.", "night", NIGHT_VOICE)
        self.assertFalse(road_report_text_path().exists())

    def test_kokoro_failure_leaves_existing_text_file_untouched(self):
        write_road_report_text("Pre-existing text.")
        self._kokoro_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("This one fails.", "day", DAY_VOICE)
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Pre-existing text.")

    def test_ffmpeg_failure_leaves_existing_text_file_untouched(self):
        write_road_report_text("Pre-existing text.")
        self._ffmpeg_should_fail = True
        with self.assertRaises(SynthesisError):
            synthesize_road_report("This one fails.", "day", DAY_VOICE)
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Pre-existing text.")

    def test_analysis_failure_leaves_existing_text_file_untouched(self):
        # The specific scenario this hardening round exists to fix: the
        # FLAC IS successfully replaced before analysis ever runs (see
        # test_analysis_failure_does_not_leave_partial_file_content_
        # mismatched above), but synthesize_road_report() must still
        # never touch road_report.txt regardless of where inside it a
        # failure occurs -- only a genuinely successful RETURN, checked
        # by the caller, ever triggers a text-file write.
        write_road_report_text("Pre-existing text.")
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(SynthesisError):
                synthesize_road_report("New content despite analysis failure.", "day", DAY_VOICE)
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Pre-existing text.")


class WriteRoadReportTextHelperTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """Direct, filesystem-only tests of write_road_report_text() itself
    -- atomic replacement behavior without going through full Kokoro/
    ffmpeg synthesis, and without overengineered failure-injection
    mocks (a real os.replace() against a real temp directory already
    exercises the actual code path)."""

    def test_creates_file_with_exact_content(self):
        write_road_report_text("Hello, KanDrive.")
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Hello, KanDrive.")

    def test_second_write_atomically_replaces_the_first(self):
        write_road_report_text("First.")
        write_road_report_text("Second.")
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Second.")

    def test_no_temp_file_left_behind(self):
        write_road_report_text("Some text.")
        leftovers = [p for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_creates_kandrive_root_if_missing(self):
        # KanDriveSynthesisFixtureMixin points KANDRIVE_ROOT at a real
        # tmpdir that already exists -- remove it to prove
        # write_road_report_text() creates it fresh, matching
        # synthesize_road_report()'s own KANDRIVE_ROOT.mkdir() call.
        Path(self._tmpdir.name).rmdir()
        write_road_report_text("Text after a fresh mkdir.")
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), "Text after a fresh mkdir.")


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
