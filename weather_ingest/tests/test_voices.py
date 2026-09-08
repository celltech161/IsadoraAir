"""lib/voices.py tests -- resolve_voice() (schedule/persona resolution,
fails closed on any malformed/missing config) and synthesize() (the
ONE place this project ever invokes the canonical shared TTS CLI).
Pure stdlib unittest + unittest.mock -- no live TTS/network calls
anywhere."""
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import voices  # noqa: E402
from voices import (  # noqa: E402
    ISADORAAIR_TTS_BINARY, REASON_LAUNCH_FAILED, REASON_MISSING_EXECUTABLE,
    REASON_NONZERO_EXIT, REASON_NO_LOGICAL_VOICE, REASON_OUTPUT_MISSING,
    REASON_TIMEOUT, VoiceResolutionError, resolve_voice, synthesize, voice_for_hour,
)


def make_cfg(schedule=None, personas=None):
    return {
        "voice_schedule": schedule if schedule is not None else [["day", 6, 17], ["night", 18, 5]],
        "voice_personas": personas if personas is not None else {
            "day": {"logical_voice": "Claira_Sky", "display_name": "Claira",
                    "full_name": "Claira Sky", "signoff": "I'm Claira Sky."},
            "night": {"logical_voice": "Max_Weatherly", "display_name": "Max",
                      "full_name": "Max Weatherly", "signoff": "I'm Max Weatherly."},
        },
    }


class VoiceForHourTests(unittest.TestCase):
    def test_day_slot_resolution(self):
        self.assertEqual(voice_for_hour(10, [["day", 6, 17], ["night", 18, 5]]), "day")

    def test_night_slot_resolution(self):
        self.assertEqual(voice_for_hour(20, [["day", 6, 17], ["night", 18, 5]]), "night")

    def test_wrapped_midnight_range_resolution(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        for hour in (18, 21, 23, 0, 5):
            with self.subTest(hour=hour):
                self.assertEqual(voice_for_hour(hour, schedule), "night")
        for hour in (6, 12, 17):
            with self.subTest(hour=hour):
                self.assertEqual(voice_for_hour(hour, schedule), "day")

    def test_no_covering_entry_defaults_to_day(self):
        self.assertEqual(voice_for_hour(10, [["night", 18, 5]]), "day")

    def test_empty_schedule_defaults_to_day(self):
        self.assertEqual(voice_for_hour(10, []), "day")


class ResolveVoiceTests(unittest.TestCase):
    def test_day_resolves_claira_sky(self):
        slot, voice = resolve_voice(make_cfg(), "day")
        self.assertEqual(slot, "day")
        self.assertEqual(voice["logical_voice"], "Claira_Sky")
        self.assertEqual(voice["name"], "Claira")
        self.assertEqual(voice["full_name"], "Claira Sky")
        self.assertEqual(voice["signoff"], "I'm Claira Sky.")

    def test_night_resolves_max_weatherly(self):
        slot, voice = resolve_voice(make_cfg(), "night")
        self.assertEqual(slot, "night")
        self.assertEqual(voice["logical_voice"], "Max_Weatherly")
        self.assertEqual(voice["name"], "Max")
        self.assertEqual(voice["full_name"], "Max Weatherly")

    def test_auto_resolves_current_schedule_correctly(self):
        cfg = make_cfg(schedule=[["day", 0, 23]])  # always day
        slot, voice = resolve_voice(cfg, "auto", now=datetime(2026, 1, 1, 15, 0))
        self.assertEqual(slot, "day")
        self.assertEqual(voice["logical_voice"], "Claira_Sky")

        cfg = make_cfg(schedule=[["night", 0, 23]])  # always night
        slot, voice = resolve_voice(cfg, "auto", now=datetime(2026, 1, 1, 15, 0))
        self.assertEqual(slot, "night")
        self.assertEqual(voice["logical_voice"], "Max_Weatherly")

    def test_auto_wrapped_midnight_range_resolution(self):
        cfg = make_cfg(schedule=[["day", 6, 17], ["night", 18, 5]])
        for hour, expected in (
            (18, "night"), (23, "night"), (0, "night"), (5, "night"), (6, "day"), (17, "day"),
        ):
            with self.subTest(hour=hour):
                slot, _voice = resolve_voice(cfg, "auto", now=datetime(2026, 1, 1, hour, 0))
                self.assertEqual(slot, expected)

    def test_missing_config_fails_closed(self):
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(None, "day")

    def test_missing_voice_schedule_for_auto_fails_closed(self):
        cfg = make_cfg()
        del cfg["voice_schedule"]
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "auto")

    def test_day_night_do_not_need_voice_schedule(self):
        """Explicit slots never consult the schedule at all -- a missing
        voice_schedule must not block a manual/test override."""
        cfg = make_cfg()
        del cfg["voice_schedule"]
        slot, voice = resolve_voice(cfg, "day")
        self.assertEqual(slot, "day")

    def test_missing_persona_for_slot_fails_closed(self):
        cfg = make_cfg(personas={})
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "day")

    def test_missing_voice_personas_entirely_fails_closed(self):
        cfg = make_cfg()
        del cfg["voice_personas"]
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "day")

    def test_malformed_persona_fails_closed(self):
        cfg = make_cfg(personas={"day": "not-a-dict"})
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "day")

    def test_missing_logical_voice_fails_closed(self):
        cfg = make_cfg(personas={"day": {"display_name": "Claira"}})
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "day")

    def test_blank_logical_voice_fails_closed(self):
        cfg = make_cfg(personas={"day": {"logical_voice": "", "display_name": "Claira"}})
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(cfg, "day")

    def test_blank_display_and_full_name_falls_back_to_slot(self):
        cfg = make_cfg(personas={"day": {"logical_voice": "Claira_Sky", "display_name": "", "full_name": ""}})
        slot, voice = resolve_voice(cfg, "day")
        self.assertEqual(voice["name"], "day")
        self.assertEqual(voice["full_name"], "day")


class SynthesizeInvocationTests(unittest.TestCase):
    """Every assertion about the ACTUAL subprocess argv/stdin this
    project constructs for the canonical CLI -- proves the contract
    exactly, and that no provider-native detail ever leaks into it."""

    def setUp(self):
        self.voice = {"logical_voice": "Claira_Sky", "name": "Claira", "full_name": "Claira Sky", "signoff": ""}
        self.exists_patcher = patch.object(voices.os.path, "exists", return_value=True)
        self.exists_patcher.start()
        self.addCleanup(self.exists_patcher.stop)
        self.makedirs_patcher = patch.object(voices.os, "makedirs")
        self.makedirs_patcher.start()
        self.addCleanup(self.makedirs_patcher.stop)

    def _run(self, run_return=None, run_side_effect=None):
        with patch.object(voices.subprocess, "run") as mock_run:
            if run_side_effect is not None:
                mock_run.side_effect = run_side_effect
            else:
                mock_run.return_value = run_return or subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"", stderr=b"",
                )
            result = synthesize("Hello listeners.", "/tmp/out.wav", self.voice)
        return result, mock_run

    def test_canonical_executable_is_exact(self):
        _result, mock_run = self._run()
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/local/bin/isadoraair-tts")
        self.assertEqual(argv[0], ISADORAAIR_TTS_BINARY)

    def test_source_text_goes_through_stdin(self):
        _result, mock_run = self._run()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["input"], "Hello listeners.".encode("utf-8"))

    def test_logical_voice_appears_in_voice_flag(self):
        _result, mock_run = self._run()
        argv = mock_run.call_args.args[0]
        self.assertIn("--voice", argv)
        self.assertEqual(argv[argv.index("--voice") + 1], "Claira_Sky")

    def test_output_path_appears_in_output_file_flag(self):
        _result, mock_run = self._run()
        argv = mock_run.call_args.args[0]
        self.assertIn("--output-file", argv)
        self.assertEqual(argv[argv.index("--output-file") + 1], "/tmp/out.wav")

    def test_explicit_timeout_is_present(self):
        _result, mock_run = self._run()
        argv = mock_run.call_args.args[0]
        self.assertIn("--timeout", argv)
        self.assertEqual(argv[argv.index("--timeout") + 1], "120")

    def test_custom_timeout_seconds_is_passed_through(self):
        with patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            synthesize("text", "/tmp/out.wav", self.voice, timeout_seconds=45)
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[argv.index("--timeout") + 1], "45")
        # Our own subprocess-level timeout has headroom above the CLI's
        # own --timeout, so the CLI can report ITS OWN timeout via exit
        # code before we'd ever kill it first.
        self.assertGreater(mock_run.call_args.kwargs["timeout"], 45)

    def test_no_provider_native_voice_ids_in_argv(self):
        _result, mock_run = self._run()
        argv_str = " ".join(mock_run.call_args.args[0])
        for marker in ("af_jessica", "am_liam", "kokoro", "Kokoro"):
            self.assertNotIn(marker, argv_str)

    def test_no_engine_flag(self):
        _result, mock_run = self._run()
        self.assertNotIn("--engine", mock_run.call_args.args[0])

    def test_no_model_flag(self):
        _result, mock_run = self._run()
        self.assertNotIn("--model", mock_run.call_args.args[0])

    def test_no_model_path_flag(self):
        _result, mock_run = self._run()
        self.assertNotIn("--model-path", mock_run.call_args.args[0])

    def test_no_voices_path_flag(self):
        _result, mock_run = self._run()
        self.assertNotIn("--voices-path", mock_run.call_args.args[0])

    def test_no_provider_executable_in_argv(self):
        _result, mock_run = self._run()
        argv_str = " ".join(mock_run.call_args.args[0])
        self.assertNotIn("kokoro_synth", argv_str)
        self.assertNotIn("/home/jreed/kokoro", argv_str)
        self.assertNotIn("piper", argv_str.lower())

    def test_no_provider_pythonpath_env_override(self):
        _result, mock_run = self._run()
        # synthesize() never passes an `env=` kwarg at all -- the
        # canonical CLI's own environment governs, this project never
        # tries to inject a provider PYTHONPATH/venv into it.
        self.assertNotIn("env", mock_run.call_args.kwargs)

    def test_arbitrary_working_directory_does_not_matter(self):
        """The canonical CLI is invoked by absolute path with no cwd=
        override -- synthesize() never depends on the caller's own
        working directory."""
        _result, mock_run = self._run()
        self.assertNotIn("cwd", mock_run.call_args.kwargs)
        argv = mock_run.call_args.args[0]
        self.assertTrue(argv[0].startswith("/"), "executable must be an absolute path")

    def test_successful_synthesis_returns_truthy_result(self):
        result, _mock_run = self._run()
        self.assertTrue(result)
        self.assertEqual(result.reason, "")


class SynthesizeFailureModeTests(unittest.TestCase):
    """Each documented failure class returns a falsy SynthesisResult
    with a distinguishing .reason -- never raises, never silently
    succeeds."""

    def setUp(self):
        self.voice = {"logical_voice": "Claira_Sky", "name": "Claira"}

    def test_missing_logical_voice_fails_without_invoking_anything(self):
        with patch.object(voices.subprocess, "run") as mock_run:
            result = synthesize("text", "/tmp/out.wav", {"name": "NoVoice"})
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_NO_LOGICAL_VOICE)
        mock_run.assert_not_called()

    def test_missing_canonical_cli_fails(self):
        with patch.object(voices.os.path, "exists", return_value=False), \
             patch.object(voices.subprocess, "run") as mock_run:
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_MISSING_EXECUTABLE)
        mock_run.assert_not_called()

    def test_timeout_fails(self):
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(
                 voices.subprocess, "run",
                 side_effect=subprocess.TimeoutExpired(cmd="isadoraair-tts", timeout=120),
             ):
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_TIMEOUT)

    def test_launch_failure_fails(self):
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.subprocess, "run", side_effect=OSError("no such file")):
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_LAUNCH_FAILED)

    def test_nonzero_exit_fails(self):
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=12, stdout=b"", stderr=b"isadoraair-tts: voice_unavailable: no such voice",
            )
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_NONZERO_EXIT)

    def test_each_documented_nonzero_class_fails_safely(self):
        # Mirrors isadoraair.tts.errors.TTSExitCode -- USAGE(2),
        # CONFIGURATION(10), RUNTIME_UNAVAILABLE(11), VOICE_UNAVAILABLE(12),
        # SYNTHESIS_FAILED(13), TIMEOUT(14). This project never branches
        # on the specific code (never scrapes stderr as an API either)
        # -- every nonzero exit is uniformly REASON_NONZERO_EXIT.
        for exit_code in (2, 10, 11, 12, 13, 14):
            with self.subTest(exit_code=exit_code):
                with patch.object(voices.os.path, "exists", return_value=True), \
                     patch.object(voices.os, "makedirs"), \
                     patch.object(voices.subprocess, "run") as mock_run:
                    mock_run.return_value = subprocess.CompletedProcess(
                        args=[], returncode=exit_code, stdout=b"", stderr=b"isadoraair-tts: some_category: detail",
                    )
                    result = synthesize("text", "/tmp/out.wav", self.voice)
                self.assertFalse(result)
                self.assertEqual(result.reason, REASON_NONZERO_EXIT)

    def test_stderr_is_never_parsed_as_an_api(self):
        """Confirms failure classification depends ONLY on the exit
        code, never on matching/parsing stderr text -- garbled or
        unexpected stderr still classifies correctly."""
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=13, stdout=b"", stderr=b"\xff\xfe garbage not utf-8 clean text",
            )
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_NONZERO_EXIT)

    def test_output_missing_after_nominal_success_fails(self):
        def fake_exists(path):
            return path == voices.ISADORAAIR_TTS_BINARY  # binary exists, wav_path does not
        with patch.object(voices.os.path, "exists", side_effect=fake_exists), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertFalse(result)
        self.assertEqual(result.reason, REASON_OUTPUT_MISSING)

    def test_failed_result_is_falsy_for_legacy_if_not_callers(self):
        """Every existing caller checks `if not voices.synthesize(...)`
        -- SynthesisResult.__bool__ must make that keep working
        unchanged even though the return value now carries more detail."""
        with patch.object(voices.os.path, "exists", return_value=False):
            result = synthesize("text", "/tmp/out.wav", self.voice)
        self.assertTrue(not result)


class SynthesizeAtomicityTests(unittest.TestCase):
    """Never deletes/truncates a pre-existing output file before the
    canonical CLI succeeds -- the historical pre-clear step is retired
    (the canonical CLI's own publication is already atomic)."""

    def test_never_removes_existing_output_before_success(self):
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.os, "remove") as mock_remove, \
             patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
            synthesize("text", "/tmp/out.wav", {"logical_voice": "Claira_Sky"})
        mock_remove.assert_not_called()

    def test_never_removes_existing_output_on_failure_either(self):
        with patch.object(voices.os.path, "exists", return_value=True), \
             patch.object(voices.os, "makedirs"), \
             patch.object(voices.os, "remove") as mock_remove, \
             patch.object(voices.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"boom")
            synthesize("text", "/tmp/out.wav", {"logical_voice": "Claira_Sky"})
        mock_remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
